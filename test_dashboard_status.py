"""Read-only dashboard diagnostics: current status must not be inferred from history."""
import os
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dashboard
import dashboard_data as dd
from test_support import TestDirectory


LIMITS = dict(SAFETY_FLOOR_USD="6", BASELINE_STAKE_USD="1", COMPOUND_STAKE_USD="2",
              GAS_RESERVE_USD="0.25", BUFFER_GATE_USD="0", ENABLE_COMPOUNDING="false")


class BudgetTests(unittest.TestCase):
    def test_reported_case_includes_stake_and_gas(self):
        result = dd.entry_budget(dict(balance_usd=6.841177433741683, state="BASELINE"), LIMITS)
        self.assertEqual(result["required_balance_usd"], 7.25)
        self.assertAlmostEqual(result["shortfall_usd"], 0.408822566258317)
        self.assertTrue(result["below_requirement"])

    def test_open_stakes_are_reserved(self):
        result = dd.entry_budget(dict(balance_usd=8.5, open_positions=[dict(stake_usd=1), dict(stake_usd=1)]), LIMITS)
        self.assertEqual(result["required_balance_usd"], 9.25)
        self.assertEqual(result["reserved_stake_usd"], 2)

    def test_raising_floor_increases_requirement(self):
        low = dd.entry_budget(dict(balance_usd=6.84), LIMITS)
        high = dd.entry_budget(dict(balance_usd=6.84), {**LIMITS, "SAFETY_FLOOR_USD": "7"})
        self.assertEqual(high["required_balance_usd"] - low["required_balance_usd"], 1)

    def test_exact_requirement_is_not_below_floor(self):
        result = dd.entry_budget(dict(balance_usd=7.25), LIMITS)
        self.assertFalse(result["below_requirement"])
        self.assertEqual(result["shortfall_usd"], 0)

    def test_halted_state_is_separate_from_funding(self):
        result = dd.entry_budget(dict(balance_usd=100, state="HALTED"), LIMITS)
        self.assertTrue(result["halted"])
        self.assertFalse(result["below_requirement"])

    def test_compound_stake_honors_opt_in_and_buffer(self):
        state = dict(balance_usd=10, state="COMPOUND")
        self.assertEqual(dd.entry_budget(state, LIMITS)["next_stake_usd"], 1)
        enabled = {**LIMITS, "ENABLE_COMPOUNDING": "true"}
        self.assertEqual(dd.entry_budget(state, enabled)["next_stake_usd"], 2)
        self.assertEqual(dd.entry_budget(state, {**enabled, "BUFFER_GATE_USD": "12"})["next_stake_usd"], 1)

    def test_missing_or_corrupt_balance_is_unknown_not_zero(self):
        for state in ({}, dict(balance_usd="nan"), dict(balance_usd=6.84, error="corrupt")):
            self.assertFalse(dd.entry_budget(state, LIMITS)["known"])

    def test_zero_floor_and_quoted_env_values_are_preserved(self):
        with _temporary() as root:
            (root / ".env").write_text('SAFETY_FLOOR_USD="0"\nBASELINE_STAKE_USD=1 # stake\nPRIVATE_KEY=test-secret\n')
            with patch.object(dd, "HERE", str(root)), patch.dict(os.environ, {}, clear=True):
                values = dd.risk_limits()
                self.assertEqual(float(values["SAFETY_FLOOR_USD"]), 0)
                self.assertEqual(float(values["BASELINE_STAKE_USD"]), 1)
                self.assertNotIn("PRIVATE_KEY", values)


from contextlib import contextmanager


@contextmanager
def _temporary():
    temporary = TestDirectory()
    try:
        yield Path(temporary.name)
    finally:
        temporary.cleanup()


class ActivityTests(unittest.TestCase):
    def test_prior_day_refusals_are_not_current_session_refusals(self):
        with _temporary() as root:
            logfile = root / "bot.log"
            logfile.write_text(
                "2026-09-14 20:46:56,000 - copytrader - INFO - Skipping trade for $OLD: Not enough balance above floor\n"
                "2026-09-15 12:07:09,000 - copytrader - INFO - Config loaded from test | exists=True | DRY_RUN=False | API_ID=set\n"
                "2026-09-15 12:07:13,000 - copytrader - INFO - Wallet Balance: 0.0012 ETH ($3.05 USD) @ $2488.70/ETH\n"
                "2026-09-15 12:11:44,000 - copytrader - DEBUG - Telegram connection heartbeat ping OK.\n")
            result = dd.activity_summary(dd.parse_logs([str(logfile)]))
            self.assertEqual(result["session_floor_refusals"], 0)
            self.assertEqual(result["session_signal_evaluations"], 0)
            self.assertEqual(result["last_floor_refusal_at"], "2026-09-14 20:46:56")
            self.assertEqual(result["last_activity_at"], "2026-09-15 12:11:44")
            self.assertEqual(result["native_wallet_usd"], 3.05)
            self.assertEqual(result["native_wallet_at"], "2026-09-15 12:07:13")

    def test_new_session_refusal_is_counted_after_startup_only(self):
        records = [dict(day="2026-09-15", time="12:00:00", kind="startup"),
                   dict(day="2026-09-15", time="12:01:00", kind="signal"),
                   dict(day="2026-09-15", time="12:01:01", kind="skip", reason="Not enough balance above floor")]
        result = dd.activity_summary(records)
        self.assertEqual(result["session_floor_refusals"], 1)
        self.assertEqual(result["session_signal_evaluations"], 1)

    def test_process_permission_failure_does_not_report_stopped(self):
        with patch.object(dd.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            self.assertIsNone(dd.bot_process()["running"])

    def test_process_success_preserves_running_and_stopped(self):
        for count in (0, 1):
            with patch.object(dd.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=str(count))):
                self.assertEqual(dd.bot_process()["running"], bool(count))


class CacheTests(unittest.TestCase):
    def setUp(self):
        dashboard.Handler._cache = {"stamp": None, "data": None}
        dashboard.Handler._process_cache = {"checked": None, "data": None}

    def test_process_refreshes_even_when_stopped_bot_cannot_update_logs(self):
        with patch.object(dashboard, "source_stamp", return_value=("unchanged",)), \
             patch.object(dashboard, "collect", return_value={"totals": {}}) as collect, \
             patch.object(dashboard, "bot_process", side_effect=[{"running": True}, {"running": False}]), \
             patch.object(dashboard.time, "monotonic", side_effect=[0, 11]):
            self.assertTrue(dashboard.Handler.current_data()["bot"]["running"])
            self.assertFalse(dashboard.Handler.current_data()["bot"]["running"])
            collect.assert_called_once_with(check_process=False)

    def test_state_and_ledger_changes_invalidate_data_without_log_changes(self):
        with _temporary() as root:
            state = root / "strategy_state.json"
            state.write_text("{}")
            with patch.object(dd, "HERE", str(root)), patch.object(dd, "LOGS", str(root)), \
                 patch.object(dd, "STATE", str(state)):
                initial = dd.source_stamp()
                state.write_text('{"balance_usd": 10}')
                updated = dd.source_stamp()
                self.assertNotEqual(initial, updated)
                (root / "events.jsonl").write_text('{}\n')
                self.assertNotEqual(updated, dd.source_stamp())


class RenderTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required to check the JavaScript renderer")
    def test_complete_page_renders_current_day_and_all_process_states(self):
        script = dashboard.PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.replace("__DATA__", "null").replace("\nboot();", "\n")
        data = dict(generated_at="2026-09-15T12:00:00", days={"2026-09-14": {"signals": 12}},
                    totals=dict(balance_usd=6.84), entry_budget=dd.entry_budget(dict(balance_usd=6.84), LIMITS),
                    activity=dict(session_signal_evaluations=0, session_floor_refusals=0))
        runner = r'''
const fs = require('fs'), vm = require('vm');
const {code, data} = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {};
const context = vm.createContext({
  document: {getElementById: id => elements[id] ||= {}, querySelectorAll: () => []},
  window: {scrollY: 0, scrollTo: () => {}}, data
});
vm.runInContext(code, context);
const states = [];
for (const running of [true, false, null]) {
  context.data.bot = {running};
  vm.runInContext('render(data)', context);
  states.push(elements.status.innerHTML);
}
process.stdout.write(JSON.stringify({html: elements.app.innerHTML, states}));
'''
        result = subprocess.run([shutil.which("node"), "-e", runner],
                                input=json.dumps(dict(code=script, data=data)),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout)
        self.assertIn("$7.25 required", rendered["html"])
        self.assertIn("$0.41", rendered["html"])
        self.assertIn("2026-09-15", rendered["html"].split("</section>", 1)[0])
        self.assertNotIn("2026-09-14", rendered["html"].split("</section>", 1)[0])
        self.assertIn("Bot process running", rendered["states"][0])
        self.assertIn("Bot not running", rendered["states"][1])
        self.assertIn("Bot status unavailable", rendered["states"][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
