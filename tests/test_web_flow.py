"""Flask 端到端流程测试：抓取示例页 → 选表 → 转换 → 下载 CSV。"""

import csv
import io
import unittest

from app.web import app


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.client = app.test_client()

    def test_index_renders(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("水库公示表格采集".encode(), resp.data)

    def test_full_flow_with_sample_page(self):
        # 1. 抓取本地标准示例
        resp = self.client.post("/fetch", data={"url": "/sample/notice.html"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("选择表格与表头行数".encode(), resp.data)

        # 从缓存中取 key（页面隐藏字段）
        html = resp.data.decode()
        key = html.split('name="cache_key" value="')[1].split('"')[0]

        # 2. 转换（2 行表头）
        resp = self.client.post(
            "/parse",
            data={"cache_key": key, "table_index": 0, "header_rows": 2},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("转换结果预览".encode(), resp.data)
        result_key = resp.data.decode().split("/download?key=")[1].split('"')[0]

        # 3. 下载 CSV
        resp = self.client.get(f"/download?key={result_key}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.startswith(b"\xef\xbb\xbf"))  # UTF-8 BOM

        text = resp.data.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(
            rows[0],
            ["水库", "表头路径", "原始文本", "来源单元格行",
             "来源单元格列", "来源单元格坐标", "来源URL"],
        )
        # 5 条正文行 × 7 个观测列 = 35 条记录
        self.assertEqual(len(rows) - 1, 35)

        # 水库名 rowspan 继承
        names = {r[0] for r in rows[1:]}
        self.assertEqual(names, {"青山水库", "碧河水库", "云岭水库"})

        # 表头路径完整且同单元格不重复
        paths = {r[1] for r in rows[1:]}
        self.assertIn("水位（米） / 旬初", paths)
        self.assertIn("入库流量（立方米每秒） / 最大", paths)

        # 空观测单元格保持为空但带来源坐标（碧河第 2 条正文行，整表第 6 行）
        empty_max = next(
            r for r in rows[1:]
            if r[0] == "碧河水库"
            and r[1] == "入库流量（立方米每秒） / 最大"
            and r[3] == "6"
        )
        self.assertEqual(empty_max[2], "")
        self.assertTrue(empty_max[5].startswith("R"))

        # 短行缺格：无来源单元格坐标，值为空
        hole = next(
            r for r in rows[1:]
            if r[0] == "云岭水库" and r[1] == "备注"
        )
        self.assertEqual(hole[2], "")
        self.assertEqual(hole[5], "")

    def test_error_sample_cross_boundary_stops_conversion(self):
        resp = self.client.post("/fetch", data={"url": "/sample/notice_error.html"})
        self.assertEqual(resp.status_code, 200)
        key = resp.data.decode().split('name="cache_key" value="')[1].split('"')[0]

        resp = self.client.post(
            "/parse",
            data={"cache_key": key, "table_index": 0, "header_rows": 2},
        )
        self.assertEqual(resp.status_code, 422)
        body = resp.data.decode()
        self.assertIn("转换已停止", body)
        self.assertIn("跨越表头与正文", body)
        self.assertIn("R1C1", body)

    def test_error_sample_illegal_body_merge(self):
        resp = self.client.post("/fetch", data={"url": "/sample/notice_error.html"})
        key = resp.data.decode().split('name="cache_key" value="')[1].split('"')[0]

        resp = self.client.post(
            "/parse",
            data={"cache_key": key, "table_index": 1, "header_rows": 2},
        )
        self.assertEqual(resp.status_code, 422)
        body = resp.data.decode()
        self.assertIn("正文非法合并", body)
        self.assertIn("R3C2", body)

    def test_missing_url_rejected(self):
        resp = self.client.post("/fetch", data={"url": ""})
        self.assertEqual(resp.status_code, 400)

    def test_expired_session_rejected(self):
        resp = self.client.post(
            "/parse",
            data={"cache_key": "nope", "table_index": 0, "header_rows": 1},
        )
        self.assertEqual(resp.status_code, 400)


class AllocationFlowTests(unittest.TestCase):
    """供水分配页端到端：转换可供水量示例页 → 选列 → 录灌区 → 求解。"""

    def setUp(self):
        app.testing = True
        self.client = app.test_client()

    def _parse_supply_sample(self):
        resp = self.client.post("/fetch", data={"url": "/sample/notice_supply.html"})
        self.assertEqual(resp.status_code, 200)
        key = resp.data.decode().split('name="cache_key" value="')[1].split('"')[0]
        resp = self.client.post(
            "/parse",
            data={"cache_key": key, "table_index": 0, "header_rows": 2},
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("供水分配", html)
        result_key = html.split("/download?key=")[1].split('"')[0]
        return result_key

    def _start_allocate(self, result_key, column=2):
        resp = self.client.post(
            "/allocate/start",
            data={"result_key": result_key, "column": column},
        )
        return resp

    def test_allocate_full_flow(self):
        result_key = self._parse_supply_sample()

        # 选择第 2 列（旬可供水量）进入供水分配页
        resp = self._start_allocate(result_key, 2)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("青山水库", html)
        self.assertIn("石门水库", html)
        self.assertIn("灌区需求录入", html)
        alloc_key = html.split('name="alloc_key" value="')[1].split('"')[0]

        # 录入 4 个灌区（含一个无水源灌区）并求解
        resp = self.client.post(
            "/allocate/solve",
            data={
                "alloc_key": alloc_key,
                "didx": ["0", "1", "2", "3"],
                "name_0": "东风灌区", "demand_0": "100",
                "src_0": ["青山水库", "碧河水库"],
                "name_1": "红旗灌区", "demand_1": "80",
                "src_1": ["碧河水库"],
                "name_2": "跃进灌区", "demand_2": "60",
                "src_2": ["云岭水库", "石门水库"],
                "name_3": "山外灌区", "demand_3": "40",
                "src_3": [],
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        # 满足率：山外 0、红旗 3/4、东风 9/10、跃进 1
        self.assertIn("9/10", body)
        self.assertIn("3/4", body)
        self.assertIn("无水源", body)
        self.assertIn("升序满足率向量", body)
        self.assertIn("分配水量", body)

        # 结果页可返回录入页，且保留上次录入
        resp = self.client.get(f"/allocate?key={alloc_key}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("东风灌区", resp.data.decode())

    def test_allocate_column_must_be_integer_values(self):
        # 标准示例页第 2 列为水位（小数），不是非负整数
        resp = self.client.post("/fetch", data={"url": "/sample/notice.html"})
        key = resp.data.decode().split('name="cache_key" value="')[1].split('"')[0]
        resp = self.client.post(
            "/parse",
            data={"cache_key": key, "table_index": 0, "header_rows": 2},
        )
        result_key = resp.data.decode().split("/download?key=")[1].split('"')[0]
        resp = self._start_allocate(result_key, 2)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("非负整数", resp.data.decode())

    def test_allocate_first_column_rejected(self):
        result_key = self._parse_supply_sample()
        resp = self._start_allocate(result_key, 1)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("水库名", resp.data.decode())

    def test_allocate_expired_sessions(self):
        resp = self.client.post(
            "/allocate/start", data={"result_key": "nope", "column": 2}
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/allocate/solve", data={"alloc_key": "nope"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/allocate?key=nope")
        self.assertEqual(resp.status_code, 400)

    def test_allocate_solve_validation_rerenders(self):
        result_key = self._parse_supply_sample()
        alloc_key = self._start_allocate(result_key, 2).data.decode().split(
            'name="alloc_key" value="'
        )[1].split('"')[0]

        # 需水量不是正整数 → 400 且回显录入页
        resp = self.client.post(
            "/allocate/solve",
            data={
                "alloc_key": alloc_key,
                "didx": ["0"],
                "name_0": "东风灌区", "demand_0": "0",
            },
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.data.decode()
        self.assertIn("正整数", body)
        self.assertIn("东风灌区", body)  # 回显已填内容

        # 灌区同名 → 400
        resp = self.client.post(
            "/allocate/solve",
            data={
                "alloc_key": alloc_key,
                "didx": ["0", "1"],
                "name_0": "同一灌区", "demand_0": "10",
                "name_1": "同一灌区", "demand_1": "20",
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("重复", resp.data.decode())

        # 一个灌区都没有 → 400
        resp = self.client.post(
            "/allocate/solve", data={"alloc_key": alloc_key, "didx": []}
        )
        self.assertEqual(resp.status_code, 400)

    def test_preview_roundtrip(self):
        result_key = self._parse_supply_sample()
        resp = self.client.get(f"/preview?key={result_key}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("转换结果预览", resp.data.decode())
        resp = self.client.get("/preview?key=nope")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
