"""Tests for the W7 budget balancer's pure coverage planner (SPEC §8)."""

from __future__ import annotations

from ynab_agent.budget.balance import (
    READY_TO_ASSIGN_SOURCE,
    BalanceOption,
    BudgetMove,
    Need,
    OptionRejection,
    Source,
    SourcePriority,
    check_moves,
    donor_slack,
    fallback_option,
    feasible_options,
    move_targets,
    need_from_assessment,
    needs_from_assessments,
    plan_coverage,
    sources_from_spends,
    validate_option,
)
from ynab_agent.budget.overspend import (
    CategorySpend,
    MonthClock,
    OverspendAssessment,
    OverspendVerdict,
)
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money


def _need(category: str, amount: str) -> Need:
    return Need(
        category=CategoryId(category), shortfall=Money.from_currency(amount)
    )


def _source(category: str, amount: str, priority: SourcePriority) -> Source:
    return Source(
        category=CategoryId(category),
        available=Money.from_currency(amount),
        priority=priority,
    )


def _move(source: str, destination: str, amount: str) -> BudgetMove:
    return BudgetMove(
        source=CategoryId(source),
        destination=CategoryId(destination),
        amount=Money.from_currency(amount),
    )


def _option(*moves: BudgetMove, label: str = "opt") -> BalanceOption:
    return BalanceOption(label=label, moves=moves, rationale="because")


def _available(*sources: Source) -> dict[CategoryId, Money]:
    return {source.category: source.available for source in sources}


def test_single_need_covered_from_one_source() -> None:
    plan = plan_coverage(
        [_need("dining", "120")],
        [_source("buffer", "500", SourcePriority.BUFFER)],
    )
    assert plan.fully_covered
    assert plan.moves == (
        BudgetMove(
            source=CategoryId("buffer"),
            destination=CategoryId("dining"),
            amount=Money.from_currency("120"),
        ),
    )


def test_sources_drawn_in_priority_order() -> None:
    # Ready-to-Assign is exhausted before the buffer is touched.
    plan = plan_coverage(
        [_need("dining", "120")],
        [
            _source("buffer", "500", SourcePriority.BUFFER),
            _source("rta", "100", SourcePriority.READY_TO_ASSIGN),
        ],
    )
    assert plan.fully_covered
    assert plan.moves[0].source == "rta"
    assert plan.moves[0].amount == Money.from_currency("100")
    assert plan.moves[1].source == "buffer"
    assert plan.moves[1].amount == Money.from_currency("20")


def test_partial_coverage_reports_uncovered() -> None:
    plan = plan_coverage(
        [_need("dining", "300")],
        [_source("rta", "100", SourcePriority.READY_TO_ASSIGN)],
    )
    assert not plan.fully_covered
    assert plan.uncovered[0].category == "dining"
    assert plan.uncovered[0].shortfall == Money.from_currency("200")


def test_one_source_split_across_two_needs() -> None:
    plan = plan_coverage(
        [_need("dining", "60"), _need("gas", "60")],
        [_source("rta", "100", SourcePriority.READY_TO_ASSIGN)],
    )
    # The first need takes $60, the second only $40 is left → $20 uncovered.
    assert plan.moves[0].destination == "dining"
    assert plan.moves[0].amount == Money.from_currency("60")
    assert plan.moves[1].destination == "gas"
    assert plan.moves[1].amount == Money.from_currency("40")
    assert plan.uncovered[0].shortfall == Money.from_currency("20")


def test_no_needs_is_an_empty_plan() -> None:
    plan = plan_coverage(
        [], [_source("rta", "100", SourcePriority.READY_TO_ASSIGN)]
    )
    assert plan.moves == ()
    assert plan.fully_covered


def test_one_rich_source_covers_a_need_above_the_ceiling() -> None:
    # A $1,200 need with a single $2,000-slack donor: the planner draws it in
    # capped chunks ($500 + $500 + $200) from that one source, fully covered —
    # never reporting it uncovered while real funds remain.
    plan = plan_coverage(
        [_need("dining", "1200")],
        [_source("rta", "2000", SourcePriority.READY_TO_ASSIGN)],
    )
    assert plan.fully_covered
    assert all(move.amount <= Money.from_currency("500") for move in plan.moves)
    assert all(move.source == "rta" for move in plan.moves)
    total = Money.zero()
    for move in plan.moves:
        total = total + move.amount
    assert total == Money.from_currency("1200")


def test_option_total_sums_its_moves() -> None:
    option = _option(
        _move("rta", "dining", "70"), _move("buffer", "dining", "50")
    )
    assert option.total == Money.from_currency("120")


def test_validate_option_accepts_a_feasible_option() -> None:
    option = _option(_move("buffer", "dining", "120"))
    available = _available(_source("buffer", "500", SourcePriority.BUFFER))
    assert validate_option(option, _need("dining", "120"), available) is None


def test_validate_option_rejects_insufficient_source() -> None:
    # The buffer only holds $80 but the move pulls $120.
    option = _option(_move("buffer", "dining", "120"))
    available = _available(_source("buffer", "80", SourcePriority.BUFFER))
    assert (
        validate_option(option, _need("dining", "120"), available)
        is OptionRejection.INSUFFICIENT_SOURCE
    )


def test_validate_option_sums_pulls_from_the_same_source() -> None:
    # Two moves from one $150 source total $160 — over what it holds.
    option = _option(
        _move("buffer", "dining", "100"), _move("buffer", "dining", "60")
    )
    available = _available(_source("buffer", "150", SourcePriority.BUFFER))
    assert (
        validate_option(option, _need("dining", "160"), available)
        is OptionRejection.INSUFFICIENT_SOURCE
    )


def test_validate_option_rejects_over_ceiling_move() -> None:
    # The $500 per-move ceiling blocks a single $600 move even if funded.
    option = _option(_move("buffer", "dining", "600"))
    available = _available(_source("buffer", "1000", SourcePriority.BUFFER))
    assert (
        validate_option(option, _need("dining", "600"), available)
        is OptionRejection.OVER_CEILING
    )


def test_validate_option_rejects_wrong_destination() -> None:
    option = _option(_move("buffer", "gas", "120"))
    available = _available(_source("buffer", "500", SourcePriority.BUFFER))
    assert (
        validate_option(option, _need("dining", "120"), available)
        is OptionRejection.WRONG_DESTINATION
    )


def test_validate_option_rejects_underfunded_plan() -> None:
    option = _option(_move("buffer", "dining", "80"))
    available = _available(_source("buffer", "500", SourcePriority.BUFFER))
    assert (
        validate_option(option, _need("dining", "120"), available)
        is OptionRejection.DOES_NOT_COVER
    )


def test_validate_option_rejects_empty_and_nonpositive() -> None:
    available = _available(_source("buffer", "500", SourcePriority.BUFFER))
    assert (
        validate_option(_option(), _need("dining", "120"), available)
        is OptionRejection.EMPTY
    )
    nonpositive = _option(_move("buffer", "dining", "0"))
    assert (
        validate_option(nonpositive, _need("dining", "120"), available)
        is OptionRejection.EMPTY
    )


def test_feasible_options_keeps_only_valid_and_preserves_order() -> None:
    sources = [
        _source("rta", "100", SourcePriority.READY_TO_ASSIGN),
        _source("buffer", "500", SourcePriority.BUFFER),
    ]
    good = _option(_move("buffer", "dining", "120"), label="good")
    underfunded = _option(_move("rta", "dining", "50"), label="underfunded")
    infeasible = _option(_move("rta", "dining", "300"), label="infeasible")
    kept = feasible_options(
        [underfunded, good, infeasible], _need("dining", "120"), sources
    )
    assert [option.label for option in kept] == ["good"]


def test_fallback_option_covers_from_priority_order() -> None:
    option = fallback_option(
        _need("dining", "120"),
        [
            _source("buffer", "500", SourcePriority.BUFFER),
            _source("rta", "100", SourcePriority.READY_TO_ASSIGN),
        ],
    )
    assert option is not None
    assert option.total == Money.from_currency("120")
    assert option.moves[0].source == "rta"


def test_fallback_option_is_none_when_money_is_short() -> None:
    option = fallback_option(
        _need("dining", "300"),
        [_source("rta", "100", SourcePriority.READY_TO_ASSIGN)],
    )
    assert option is None


def _spend(
    category: str, budgeted: str, activity: str, balance: str
) -> CategorySpend:
    return CategorySpend(
        category=CategoryId(category),
        name=category.title(),
        budgeted=Money.from_currency(budgeted),
        activity=Money.from_currency(activity),
        balance=Money.from_currency(balance),
    )


# Mid-month, so a donor's run-rate is a credible signal of its own trajectory.
_CLOCK = MonthClock(day_of_month=15, days_in_month=30)


def test_need_from_assessment_sizes_to_available() -> None:
    # $520 projected, $250 spent → $270 still to spend; $150 available → a $120
    # shortfall (what keeps available from going negative).
    assessment = OverspendAssessment(
        category=CategoryId("dining"),
        name="Dining",
        verdict=OverspendVerdict.TRENDING_OVER,
        budgeted=Money.from_currency("400"),
        spent=Money.from_currency("250"),
        projected=Money.from_currency("520"),
        available=Money.from_currency("150"),
    )
    need = need_from_assessment(assessment)
    assert need.category == "dining"
    assert need.shortfall == Money.from_currency("120")


def test_need_from_assessment_rollover_cushions_the_shortfall() -> None:
    # Same projection over budget, but $400 available (rollover): the remaining
    # $270 of spend is fully covered, so there is no real need.
    assessment = OverspendAssessment(
        category=CategoryId("dining"),
        name="Dining",
        verdict=OverspendVerdict.TRENDING_OVER,
        budgeted=Money.from_currency("400"),
        spent=Money.from_currency("250"),
        projected=Money.from_currency("520"),
        available=Money.from_currency("400"),
    )
    assert need_from_assessment(assessment).shortfall == Money.zero()


def _over(
    name: str, *, spent: str, projected: str, available: str
) -> OverspendAssessment:
    return OverspendAssessment(
        category=CategoryId(name),
        name=name.title(),
        verdict=OverspendVerdict.TRENDING_OVER,
        budgeted=Money.from_currency("400"),
        spent=Money.from_currency(spent),
        projected=Money.from_currency(projected),
        available=Money.from_currency(available),
    )


def test_needs_from_assessments_orders_biggest_first_and_drops_covered() -> (
    None
):
    # dining needs $120, gas needs $40, rollover is fully covered (need 0) → the
    # coordinated pass covers the biggest gap first, and drops the covered one.
    dining = _over("dining", spent="250", projected="520", available="150")
    gas = _over("gas", spent="150", projected="240", available="50")
    covered = _over("rollover", spent="250", projected="520", available="400")
    needs = needs_from_assessments([gas, dining, covered])
    assert [str(need.category) for need in needs] == ["dining", "gas"]
    assert needs[0].shortfall == Money.from_currency("120")
    assert needs[1].shortfall == Money.from_currency("40")


def test_need_from_assessment_floors_at_zero() -> None:
    assessment = OverspendAssessment(
        category=CategoryId("dining"),
        name="Dining",
        verdict=OverspendVerdict.OK,
        budgeted=Money.from_currency("400"),
        spent=Money.from_currency("100"),
        projected=Money.from_currency("300"),
        available=Money.from_currency("300"),
    )
    assert need_from_assessment(assessment).shortfall == Money.zero()


def test_sources_from_spends_includes_rta_and_donors_with_slack() -> None:
    sources = sources_from_spends(
        [
            _spend("dining", "400", "-420", "-20"),  # the needy one, excluded
            _spend("buffer", "500", "0", "500"),  # untouched → full slack
            _spend("rent", "1500", "-1500", "0"),  # spent it all → no slack
        ],
        Money.from_currency("100"),
        exclude=frozenset({CategoryId("dining")}),
        clock=_CLOCK,
    )
    by_id = {str(s.category): s for s in sources}
    assert str(READY_TO_ASSIGN_SOURCE) in by_id
    assert by_id[str(READY_TO_ASSIGN_SOURCE)].available == Money.from_currency(
        "100"
    )
    assert "buffer" in by_id
    assert "dining" not in by_id  # excluded
    assert "rent" not in by_id  # no slack


def test_sources_excludes_a_donor_heading_over_itself() -> None:
    # Spent $380 of $400, projecting ~$580: it's trending over its own budget,
    # so it has no slack and is never offered as a donor (the #45 fix).
    sources = sources_from_spends(
        [_spend("groceries", "400", "-380", "20")],
        Money.zero(),
        exclude=frozenset({CategoryId("dining")}),
        clock=_CLOCK,
    )
    assert sources == ()


def test_donor_slack_keeps_a_rollover_funded_category() -> None:
    # budgeted $0 but $200 available (carryover), $50 spent and ~$75 projected →
    # only $25 of remaining spend to protect, so it can spare ~$175. Anchoring
    # to budgeted would wrongly refuse the safest donor there is.
    slack, projection = donor_slack(
        _spend("vacation", "0", "-50", "200"), _CLOCK
    )
    assert projection == Money.from_currency("75")
    assert slack == Money.from_currency("175")


def test_donor_slack_excludes_a_thin_balance_over_against_available() -> None:
    # High budget ($1000) but a thin $100 balance (negative carryover): $400
    # spent, ~$733 projected → ~$333 of remaining spend against only $100, so it
    # is itself trending over against *available*. Measuring "over" vs budgeted
    # ($733 < $1000) would call it flush and hand over its whole $100; slack vs
    # available correctly gives 0, so it is never offered as a donor.
    slack, _ = donor_slack(_spend("groceries", "1000", "-400", "100"), _CLOCK)
    assert slack == Money.zero()


def test_sources_ranked_by_slack_with_rta_leading() -> None:
    # RTA leads (priority); within real categories, the higher-slack donor is
    # drained first — replacing the old UUID tiebreak.
    sources = sources_from_spends(
        [
            _spend("small", "100", "0", "100"),  # ~$100 slack
            _spend("buffer", "500", "0", "500"),  # ~$500 slack
        ],
        Money.from_currency("80"),
        exclude=frozenset({CategoryId("dining")}),
        clock=_CLOCK,
    )
    plan = plan_coverage([_need("dining", "650")], list(sources))
    assert [str(move.source) for move in plan.moves] == [
        str(READY_TO_ASSIGN_SOURCE),
        "buffer",
        "small",
    ]


def test_sources_from_spends_omits_rta_when_zero() -> None:
    sources = sources_from_spends(
        [_spend("buffer", "500", "0", "500")],
        Money.zero(),
        exclude=frozenset({CategoryId("dining")}),
        clock=_CLOCK,
    )
    assert all(s.category != READY_TO_ASSIGN_SOURCE for s in sources)


def test_validate_option_rejects_pulling_a_donor_below_its_slack() -> None:
    # The buffer holds $500 but can only spare $200 (its own spend protected);
    # a $300 pull is funded yet over-slack → rejected with the SLACK veto.
    option = _option(_move("buffer", "dining", "300"))
    available = {CategoryId("buffer"): Money.from_currency("500")}
    slack = {CategoryId("buffer"): Money.from_currency("200")}
    assert (
        validate_option(option, _need("dining", "300"), available, slack=slack)
        is OptionRejection.SLACK
    )


def test_check_moves_rejects_pulling_a_donor_below_its_slack() -> None:
    moves = (_move("buffer", "dining", "300"),)
    available = {CategoryId("buffer"): Money.from_currency("500")}
    slack = {CategoryId("buffer"): Money.from_currency("200")}
    assert check_moves(moves, available, slack=slack) is OptionRejection.SLACK


def test_feasible_options_drops_an_over_slack_option() -> None:
    # The source has the money ($500) but only $100 of slack; an option pulling
    # $150 is dropped by the guard via each source's drawable slack.
    sources = [
        Source(
            category=CategoryId("buffer"),
            available=Money.from_currency("500"),
            priority=SourcePriority.OVERFUNDED,
            slack=Money.from_currency("100"),
        )
    ]
    over = _option(_move("buffer", "dining", "150"))
    assert feasible_options([over], _need("dining", "150"), sources) == ()


def test_check_moves_allows_a_funded_partial_cover() -> None:
    # Owner chose to cover only $50 of a larger shortfall — that's allowed.
    moves = (_move("buffer", "dining", "50"),)
    available = {CategoryId("buffer"): Money.from_currency("500")}
    assert check_moves(moves, available) is None


def test_check_moves_rejects_insufficient_and_over_ceiling() -> None:
    available = {CategoryId("buffer"): Money.from_currency("40")}
    assert (
        check_moves((_move("buffer", "dining", "120"),), available)
        is OptionRejection.INSUFFICIENT_SOURCE
    )
    big = {CategoryId("buffer"): Money.from_currency("1000")}
    assert (
        check_moves((_move("buffer", "dining", "600"),), big)
        is OptionRejection.OVER_CEILING
    )


def test_move_targets_raises_destination_and_lowers_source() -> None:
    moves = (_move("buffer", "dining", "120"),)
    current = {
        CategoryId("buffer"): Money.from_currency("500"),
        CategoryId("dining"): Money.from_currency("400"),
    }
    targets = move_targets(moves, current)
    assert targets[CategoryId("dining")] == Money.from_currency("520")
    assert targets[CategoryId("buffer")] == Money.from_currency("380")


def test_move_targets_skips_the_rta_sentinel_source() -> None:
    # A move from Ready-to-Assign raises the destination; RTA isn't written.
    moves = (
        BudgetMove(
            source=READY_TO_ASSIGN_SOURCE,
            destination=CategoryId("dining"),
            amount=Money.from_currency("100"),
        ),
    )
    current = {CategoryId("dining"): Money.from_currency("400")}
    targets = move_targets(moves, current)
    assert targets == {CategoryId("dining"): Money.from_currency("500")}
    assert READY_TO_ASSIGN_SOURCE not in targets


def test_greedy_fallback_caps_each_move_at_the_floor_ceiling() -> None:
    # One source could cover the whole $700 shortfall, but a single $700 move
    # breaches the $500 per-move ceiling the floor would refuse to apply —
    # the fallback must never offer a plan the agent's own floor vetoes.
    plan = plan_coverage(
        [
            Need(
                category=CategoryId("dining"),
                shortfall=Money.from_currency("700"),
            )
        ],
        [
            Source(
                category=CategoryId("buffer"),
                available=Money.from_currency("1000"),
                priority=SourcePriority.BUFFER,
            ),
            Source(
                category=CategoryId("fun"),
                available=Money.from_currency("400"),
                priority=SourcePriority.OVERFUNDED,
            ),
        ],
    )
    assert all(move.amount <= Money.from_currency("500") for move in plan.moves)
    total = Money.zero()
    for move in plan.moves:
        total = total + move.amount
    assert total == Money.from_currency("700")  # still fully covered
    assert plan.uncovered == ()
