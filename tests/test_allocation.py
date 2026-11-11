"""供水分配核心测试：可供水量列解析、灌区校验、字典序最大满足率求解。"""

import unittest
from fractions import Fraction

from app.allocation import (
    MAX_DISTRICTS,
    MAX_RESERVOIRS,
    AllocationError,
    District,
    Reservoir,
    parse_supply_column,
    solve_allocation,
    validate_districts,
)


def rec(reservoir, value, col, row=1, cell=""):
    return {
        "reservoir": reservoir,
        "header_path": "",
        "value": value,
        "source_row": row,
        "source_col": col,
        "source_cell": cell,
        "source_url": "",
    }


class SupplyColumnTests(unittest.TestCase):
    def test_parse_ok(self):
        records = [
            rec("青山水库", "90", 2, 3, "R3C2"),
            rec("碧河水库", "60", 2, 4, "R4C2"),
            rec("云岭水库", "0", 2, 5, "R5C2"),
        ]
        res = parse_supply_column(records, 2, 4)
        self.assertEqual(
            res,
            [Reservoir("青山水库", 90), Reservoir("碧河水库", 60),
             Reservoir("云岭水库", 0)],
        )

    def test_column_out_of_range(self):
        records = [rec("甲库", "10", 2)]
        with self.assertRaises(AllocationError):
            parse_supply_column(records, 1, 4)  # 首列是水库名
        with self.assertRaises(AllocationError):
            parse_supply_column(records, 5, 4)
        with self.assertRaises(AllocationError):
            parse_supply_column(records, 0, 4)

    def test_non_integer_rejected(self):
        records = [rec("青山水库", "86.20", 2, 3, "R3C2")]
        with self.assertRaises(AllocationError) as ctx:
            parse_supply_column(records, 2, 4)
        self.assertIn("非负整数", str(ctx.exception))
        self.assertIn("青山水库", str(ctx.exception))
        self.assertIn("R3C2", str(ctx.exception))

    def test_empty_and_negative_rejected(self):
        with self.assertRaises(AllocationError):
            parse_supply_column([rec("甲库", "", 2)], 2, 3)
        with self.assertRaises(AllocationError):
            parse_supply_column([rec("甲库", "-5", 2)], 2, 3)

    def test_duplicate_reservoir_rejected(self):
        records = [
            rec("青山水库", "90", 2, 3),
            rec("青山水库", "80", 2, 4),  # rowspan 继承导致的同名
        ]
        with self.assertRaises(AllocationError) as ctx:
            parse_supply_column(records, 2, 4)
        self.assertIn("同名水库", str(ctx.exception))

    def test_too_many_reservoirs_rejected(self):
        records = [rec(f"库{i}", "10", 2) for i in range(MAX_RESERVOIRS + 1)]
        with self.assertRaises(AllocationError) as ctx:
            parse_supply_column(records, 2, 3)
        self.assertIn(str(MAX_RESERVOIRS), str(ctx.exception))

    def test_empty_column_rejected(self):
        with self.assertRaises(AllocationError):
            parse_supply_column([], 2, 3)


class DistrictValidationTests(unittest.TestCase):
    NAMES = ["甲库", "乙库"]

    def test_ok_and_no_source_allowed(self):
        ds = validate_districts(
            [("东风灌区", "100", ["甲库", "乙库"]), ("山外灌区", "40", [])],
            self.NAMES,
        )
        self.assertEqual(ds[0], District("东风灌区", 100, ("甲库", "乙库")))
        self.assertEqual(ds[1].sources, ())

    def test_empty_rows_rejected(self):
        with self.assertRaises(AllocationError):
            validate_districts([], self.NAMES)

    def test_too_many_rejected(self):
        rows = [(f"灌区{i}", "10", []) for i in range(MAX_DISTRICTS + 1)]
        with self.assertRaises(AllocationError) as ctx:
            validate_districts(rows, self.NAMES)
        self.assertIn(str(MAX_DISTRICTS), str(ctx.exception))

    def test_bad_name_and_duplicate(self):
        with self.assertRaises(AllocationError):
            validate_districts([("  ", "10", [])], self.NAMES)
        with self.assertRaises(AllocationError):
            validate_districts(
                [("甲灌区", "10", []), ("甲灌区", "20", [])], self.NAMES
            )

    def test_demand_must_be_positive_integer(self):
        for bad in ["0", "-3", "1.5", "", "abc"]:
            with self.assertRaises(AllocationError):
                validate_districts([("灌区", bad, [])], self.NAMES)

    def test_unknown_source_rejected(self):
        with self.assertRaises(AllocationError):
            validate_districts([("灌区", "10", ["丙库"])], self.NAMES)


def demo_problem():
    """与内置可供水量示例页 + 演示灌区一致的算例。"""
    reservoirs = [
        Reservoir("青山水库", 90),
        Reservoir("碧河水库", 60),
        Reservoir("云岭水库", 45),
        Reservoir("石门水库", 35),
    ]
    districts = [
        District("东风灌区", 100, ("青山水库", "碧河水库")),
        District("红旗灌区", 80, ("碧河水库",)),
        District("跃进灌区", 60, ("云岭水库", "石门水库")),
        District("山外灌区", 40, ()),
    ]
    return reservoirs, districts


class SolveTests(unittest.TestCase):
    def assert_flows_consistent(self, reservoirs, districts, result):
        """分配矩阵须可行且复现满足率。"""
        m, n = len(reservoirs), len(districts)
        for r in range(m):
            row_sum = sum(result.flows[r][i] for i in range(n))
            self.assertLessEqual(row_sum, reservoirs[r].supply)
            self.assertEqual(row_sum, result.reservoir_used[r])
        for i in range(n):
            col_sum = sum(result.flows[r][i] for r in range(m))
            self.assertEqual(col_sum, result.received[i])
            self.assertEqual(result.received[i], result.rates[i] * districts[i].demand)
            self.assertLessEqual(result.received[i], districts[i].demand)
            self.assertEqual(
                result.gaps[i], districts[i].demand - result.received[i]
            )
            # 水只能沿指定关系分配
            for r in range(m):
                if result.flows[r][i] > 0:
                    self.assertIn(reservoirs[r].name, districts[i].sources)

    def test_demo_case(self):
        reservoirs, districts = demo_problem()
        result = solve_allocation(reservoirs, districts)
        rates = dict(zip([d.name for d in districts], result.rates))
        self.assertEqual(rates["山外灌区"], Fraction(0))  # 无水源为 0
        self.assertEqual(rates["红旗灌区"], Fraction(3, 4))
        self.assertEqual(rates["东风灌区"], Fraction(9, 10))
        self.assertEqual(rates["跃进灌区"], Fraction(1))
        # 升序向量
        self.assertEqual(
            [r for _, r in result.sorted_rates],
            [Fraction(0), Fraction(3, 4), Fraction(9, 10), Fraction(1)],
        )
        # 红旗只能由碧河供水，60/80 = 3/4 被强制
        self.assertEqual(result.flows[1][1], Fraction(60))
        self.assert_flows_consistent(reservoirs, districts, result)

    def test_single_reservoir_split_evenly(self):
        reservoirs = [Reservoir("甲库", 10)]
        districts = [
            District("A", 10, ("甲库",)),
            District("B", 30, ("甲库",)),
        ]
        result = solve_allocation(reservoirs, districts)
        self.assertEqual(result.rates, [Fraction(1, 4), Fraction(1, 4)])
        self.assert_flows_consistent(reservoirs, districts, result)

    def test_chain_two_levels(self):
        # 甲库只供 A 且量大，乙库同时供 A、B 但量小
        reservoirs = [Reservoir("甲库", 10), Reservoir("乙库", 1)]
        districts = [
            District("A", 4, ("甲库", "乙库")),
            District("B", 4, ("乙库",)),
        ]
        result = solve_allocation(reservoirs, districts)
        self.assertEqual(result.rates, [Fraction(1), Fraction(1, 4)])
        self.assert_flows_consistent(reservoirs, districts, result)

    def test_three_levels(self):
        # 三层满足率：A=1，B=1/2，C=1/4
        reservoirs = [
            Reservoir("R1", 10),
            Reservoir("R2", 2),
            Reservoir("R3", 1),
        ]
        districts = [
            District("A", 4, ("R1", "R2", "R3")),
            District("B", 4, ("R2", "R3")),
            District("C", 4, ("R3",)),
        ]
        result = solve_allocation(reservoirs, districts)
        self.assertEqual(result.rates[0], Fraction(1))
        self.assertEqual(result.rates[1], Fraction(1, 2))
        self.assertEqual(result.rates[2], Fraction(1, 4))
        self.assert_flows_consistent(reservoirs, districts, result)

    def test_three_levels_scarce(self):
        reservoirs = [
            Reservoir("R1", 2),
            Reservoir("R2", 2),
            Reservoir("R3", 2),
        ]
        districts = [
            District("A", 4, ("R1", "R2", "R3")),
            District("B", 4, ("R2", "R3")),
            District("C", 8, ("R3",)),
        ]
        result = solve_allocation(reservoirs, districts)
        # C 只能拿 R3：2/8 = 1/4；B 拿 R2：2/4 = 1/2；A 拿 R1：2/4 = 1/2
        self.assertEqual(result.rates[2], Fraction(1, 4))
        self.assertEqual(result.rates[1], Fraction(1, 2))
        self.assertEqual(result.rates[0], Fraction(1, 2))
        self.assert_flows_consistent(reservoirs, districts, result)

    def test_all_satisfied_when_abundant(self):
        reservoirs = [Reservoir("甲库", 1000)]
        districts = [District("A", 10, ("甲库",)), District("B", 5, ("甲库",))]
        result = solve_allocation(reservoirs, districts)
        self.assertEqual(result.rates, [Fraction(1), Fraction(1)])
        self.assertEqual(result.gaps, [Fraction(0), Fraction(0)])

    def test_no_source_still_optimizes_others(self):
        reservoirs = [Reservoir("甲库", 10)]
        districts = [
            District("无源", 7, ()),
            District("有源", 10, ("甲库",)),
        ]
        result = solve_allocation(reservoirs, districts)
        self.assertEqual(result.rates, [Fraction(0), Fraction(1)])
        self.assertEqual(result.received, [Fraction(0), Fraction(10)])
        self.assert_flows_consistent(reservoirs, districts, result)

    def test_zero_supply_reservoir(self):
        reservoirs = [Reservoir("空库", 0), Reservoir("甲库", 6)]
        districts = [
            District("A", 4, ("空库",)),
            District("B", 4, ("甲库",)),
        ]
        result = solve_allocation(reservoirs, districts)
        self.assertEqual(result.rates, [Fraction(0), Fraction(1)])
        self.assert_flows_consistent(reservoirs, districts, result)

    def test_lex_order_beats_greedy(self):
        # 若先灌满 A（10），B 只剩 5 → (1/2, 1)；字典序最大应为 (3/4, 3/4)
        reservoirs = [Reservoir("甲库", 5), Reservoir("乙库", 10)]
        districts = [
            District("A", 10, ("甲库", "乙库")),
            District("B", 10, ("乙库",)),
        ]
        result = solve_allocation(reservoirs, districts)
        self.assertEqual(result.rates, [Fraction(3, 4), Fraction(3, 4)])
        self.assert_flows_consistent(reservoirs, districts, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
