"""Tests for ``run_targets``: which workspaces ``explain_run`` searches for a run. Pure; no I/O.

Runs standalone (``python test_databricks_jobs_locate.py``).
"""

from __future__ import annotations

from databricks_test_support import run_tests

from v1.core.databricks_workspaces import Workspace
from v1.core.tools.databricks.jobs import tools as jobs_tools
from v1.core.tools.databricks.jobs.locate import (
    HIDDEN_LINK_MESSAGE,
    HIDDEN_NAMED_MESSAGE,
    JOBS_DISABLED_MESSAGE,
    LINK_NOT_IN_NAMED_MESSAGE,
    Targets,
    run_targets,
)
from v1.core.tools.databricks.sql.scope import DataScope


def _ws(alias: str, label: str, host: str, *, jobs: bool = True) -> Workspace:
    return Workspace(
        alias=alias,
        label=label,
        host=host,
        sql_warehouse_id="wh-1",
        data_allowlist=None,
        scope=DataScope(),
        jobs_enabled=jobs,
        groups=(),
    )


dev = _ws("dev", "Dev", "https://adb-1.azuredatabricks.net")
rpl = _ws("rpl", "Replica", "https://adb-2.azuredatabricks.net/")
prod = _ws("prod-ws", "Prod", "https://adb-3.azuredatabricks.net")
dev_off = _ws("dev", "Dev", "https://adb-1.azuredatabricks.net", jobs=False)
rpl_off = _ws("rpl", "Replica", "https://adb-2.azuredatabricks.net", jobs=False)
prod_off = _ws("prod-ws", "Prod", "https://adb-3.azuredatabricks.net", jobs=False)


def _link(host: str, run: int = 456) -> str:
    return f"https://{host}/jobs/1/runs/{run}"


# -- exact copy ---------------------------------------------------------------


def test_messages_are_the_exact_copy():
    assert HIDDEN_LINK_MESSAGE == "That run link is from a Databricks workspace you can't use here."
    assert HIDDEN_NAMED_MESSAGE == "That isn't a Databricks workspace you can use here."
    assert LINK_NOT_IN_NAMED_MESSAGE == "That run link isn't from {label}."
    assert JOBS_DISABLED_MESSAGE == "Job lookup isn't enabled for {label}."


def test_jobs_disabled_message_still_importable_from_tools():
    assert jobs_tools.JOBS_DISABLED_MESSAGE is JOBS_DISABLED_MESSAGE


# -- bare run ID --------------------------------------------------------------


def test_bare_id_current_first_then_others():
    t = run_targets("456", None, dev, [dev, rpl, prod])
    assert isinstance(t, Targets)
    assert t.first is dev and t.others == (rpl, prod) and t.refusal is None


def test_bare_id_current_not_first_in_visible_order():
    t = run_targets("456", None, prod, [dev, rpl, prod])
    assert t.first is prod and t.others == (dev, rpl) and t.refusal is None


def test_bare_id_current_jobs_off_searches_others():
    t = run_targets("456", None, dev_off, [dev_off, rpl])
    assert t.first is None and t.others == (rpl,) and t.refusal is None


def test_bare_id_skips_jobs_off_others():
    t = run_targets("456", None, dev, [dev, rpl_off, prod])
    assert t.first is dev and t.others == (prod,) and t.refusal is None


def test_bare_id_current_only():
    t = run_targets("456", None, dev, [dev])
    assert t == Targets(dev, (), None)


def test_bare_id_no_candidates_is_current_jobs_disabled():
    t = run_targets("456", None, dev_off, [dev_off, rpl_off])
    assert t.first is None and t.others == ()
    assert t.refusal == "Job lookup isn't enabled for Dev." and t.refused_in is dev_off


def test_bare_id_nothing_visible_is_current_jobs_disabled():
    t = run_targets("456", None, dev, [])
    assert t == Targets(None, (), "Job lookup isn't enabled for Dev.", dev)


def test_malformed_ref_is_treated_like_a_bare_id():
    # explain_run rejects malformed refs itself; here they just get the fan-out.
    t = run_targets("not a run", None, dev, [dev, rpl])
    assert t == Targets(dev, (rpl,), None)


def test_http_link_is_not_a_link():
    t = run_targets("http://adb-2.azuredatabricks.net/jobs/1/runs/456", None, dev, [dev, rpl])
    assert t == Targets(dev, (rpl,), None)


# -- run link -----------------------------------------------------------------


def test_link_resolves_by_host_case_and_query_insensitive():
    for url in (
        "https://ADB-2.azuredatabricks.net/jobs/1/runs/456",
        "https://adb-2.azuredatabricks.net/jobs/1/runs/456/?o=99",
        "https://adb-2.azuredatabricks.net/?o=99#job/1/run/456",
    ):
        t = run_targets(url, None, dev, [dev, rpl])
        assert t.first is rpl and t.others == () and t.refusal is None, url


def test_link_to_current_workspace():
    t = run_targets(_link("adb-1.azuredatabricks.net"), None, dev, [dev, rpl])
    assert t == Targets(dev, (), None)


def test_link_matches_host_stored_without_scheme_or_with_case():
    odd = _ws("odd", "Odd", "ADB-9.azuredatabricks.net/")
    t = run_targets(_link("adb-9.azuredatabricks.net"), None, dev, [dev, odd])
    assert t.first is odd and t.others == () and t.refusal is None


def test_link_to_hidden_host_names_nothing():
    t = run_targets(_link("adb-77.azuredatabricks.net"), None, dev, [dev, rpl])
    assert t.first is None and t.others == ()
    assert t.refusal == "That run link is from a Databricks workspace you can't use here."
    assert t.refused_in is None


def test_link_to_visible_jobs_off_workspace():
    t = run_targets(_link("adb-2.azuredatabricks.net"), None, dev, [dev, rpl_off])
    assert t == Targets(None, (), "Job lookup isn't enabled for Replica.", rpl_off)


def test_link_to_current_jobs_off():
    t = run_targets(_link("adb-1.azuredatabricks.net"), None, dev_off, [dev_off, rpl])
    assert t == Targets(None, (), "Job lookup isn't enabled for Dev.", dev_off)


def test_link_host_shared_by_jobs_on_and_off_entries_uses_jobs_on():
    twin_off = _ws("twin", "Twin", "https://adb-2.azuredatabricks.net", jobs=False)
    t = run_targets(_link("adb-2.azuredatabricks.net"), None, dev, [dev, twin_off, rpl])
    assert t == Targets(rpl, (), None)


# -- named workspace ----------------------------------------------------------


def test_named_by_label_or_alias_any_case():
    for name in ("PROD", "prod", "prod-ws", "Prod-WS"):
        t = run_targets("456", name, dev, [dev, rpl, prod])
        assert t == Targets(prod, (), None), name


def test_blank_name_is_no_name():
    for blank in ("", "   ", "	"):
        for run in ("456", _link("adb-2.azuredatabricks.net"), _link("adb-77.azuredatabricks.net")):
            assert run_targets(run, blank, dev, [dev, rpl]) == run_targets(
                run, None, dev, [dev, rpl]
            ), (blank, run)


def test_named_alias_beats_another_workspaces_label():
    # A's label is B's alias: the name means B.
    a = _ws("alpha", "beta", "https://adb-1.azuredatabricks.net")
    b = _ws("beta", "Bravo", "https://adb-2.azuredatabricks.net")
    for name in ("beta", "BETA"):
        assert run_targets("456", name, a, [a, b]) == Targets(b, (), None), name
    # No alias matches: the label still resolves.
    assert run_targets("456", "Bravo", a, [a, b]) == Targets(b, (), None)


def test_named_current_workspace():
    t = run_targets("456", "dev", dev, [dev, rpl])
    assert t == Targets(dev, (), None)


def test_named_hidden_is_refused():
    t = run_targets("456", "secret", dev, [dev, rpl])
    assert t.first is None and t.others == ()
    assert t.refusal == "That isn't a Databricks workspace you can use here."
    assert t.refused_in is None


def test_named_partial_match_is_not_a_match():
    t = run_targets("456", "pro", dev, [dev, prod])
    assert t.refusal == HIDDEN_NAMED_MESSAGE


def test_named_jobs_off_is_refused():
    t = run_targets("456", "Prod", dev, [dev, prod_off])
    assert t == Targets(None, (), "Job lookup isn't enabled for Prod.", prod_off)


def test_named_with_its_own_link():
    t = run_targets(_link("ADB-3.azuredatabricks.net"), "prod", dev, [dev, prod])
    assert t == Targets(prod, (), None)


def test_named_with_other_hosts_link_is_refused():
    t = run_targets(_link("adb-1.azuredatabricks.net"), "prod", dev, [dev, prod])
    assert t.first is None and t.others == ()
    assert t.refusal == "That run link isn't from Prod." and t.refused_in is prod


def test_named_with_hidden_hosts_link_names_only_the_named():
    t = run_targets(_link("adb-77.azuredatabricks.net"), "prod", dev, [dev, prod])
    assert t.refusal == "That run link isn't from Prod."


def test_named_resolves_to_its_deduplicated_representative():
    dev_b = _ws("dev-b", "Dev B", "https://ADB-1.azuredatabricks.net/")
    t = run_targets("456", "dev-b", dev, [dev_b, dev, rpl])
    assert t == Targets(dev, (), None)


def test_named_jobs_off_twin_of_jobs_on_host_uses_the_jobs_on_entry():
    # The named entry has jobs off, but its host is reachable through a jobs-on entry:
    # the named entry's own setting decides.
    twin_off = _ws("twin", "Twin", "https://adb-2.azuredatabricks.net", jobs=False)
    t = run_targets("456", "twin", dev, [dev, rpl, twin_off])
    assert t == Targets(None, (), "Job lookup isn't enabled for Twin.", twin_off)


# -- shared hosts -------------------------------------------------------------


def test_shared_host_deduplicated_current_wins():
    dev_b = _ws("dev-b", "Dev B", "https://ADB-1.azuredatabricks.net/")
    t = run_targets("456", None, dev, [dev_b, rpl, dev])
    assert t == Targets(dev, (rpl,), None)


def test_shared_host_not_current_keeps_first_in_order():
    rpl_b = _ws("rpl-b", "Replica B", "adb-2.azuredatabricks.net")
    t = run_targets("456", None, dev, [dev, rpl, rpl_b, prod])
    assert t == Targets(dev, (rpl, prod), None)
    t = run_targets("456", None, dev, [dev, rpl_b, rpl, prod])
    assert t == Targets(dev, (rpl_b, prod), None)


def test_shared_host_link_returns_representative():
    rpl_b = _ws("rpl-b", "Replica B", "adb-2.azuredatabricks.net")
    t = run_targets(_link("adb-2.azuredatabricks.net"), None, dev, [dev, rpl_b, rpl])
    assert t == Targets(rpl_b, (), None)


def test_current_jobs_off_shares_host_with_jobs_on_entry():
    # A jobs-off chat workspace is never searched first, even when another visible
    # jobs-on entry shares its host: that entry is just part of the fan-out.
    dev_b = _ws("dev-b", "Dev B", "https://adb-1.azuredatabricks.net")
    t = run_targets("456", None, dev_off, [dev_off, dev_b, rpl])
    assert t == Targets(None, (dev_b, rpl), None)


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
