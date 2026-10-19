"""Guards that used to be ~70 regex lines in windows-beta.yml's "Validate source" step (plan, Packaging:
"a change-detector, not a test suite"). Each guard is now a named test: behaviour where it can run without
MetaTrader (CLI, constants, heartbeat payload, resident-EA path), otherwise a static check on the source that
says which fix it protects. Discovery/pairing/compile-log behaviour is exercised by `preview_bridge.py self-test`.
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import preview_bridge as pb  # noqa: E402


def _read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def bridge_source():
    """All preview-bridge Python source, so the guards survive moving code between its modules."""
    return _read('preview_bridge.py')


class BridgeCli(unittest.TestCase):
    """The Tauri app and the batch drive these subcommands and flags."""

    def parse(self, *argv):
        return pb.build_parser().parse_args(list(argv))

    def test_preflight(self):
        self.assertEqual(self.parse('preflight', '--source', 's', '--out', 'o').cmd, 'preflight')

    def test_render_server_with_account_and_runtime_generation(self):
        x = self.parse('render-server', '--db', 'd', '--out', 'o', '--mt5-data-dir', 'm', '--runtime-id', 'r')
        self.assertEqual((x.mt5_data_dir, x.runtime_id, x.hang_timeout), ('m', 'r', 40))  # FIX 1, 4802 fix

    def test_compile_pool_with_runtime_generation(self):
        x = self.parse('compile-pool', '--db', 'd', '--out', 'o', '--runtime-id', 'r')
        self.assertEqual((x.cmd, x.runtime_id), ('compile-pool', 'r'))

    def test_preview_selftest(self):
        self.assertEqual(self.parse('preview-selftest', '--out', 'o').cmd, 'preview-selftest')

    def test_detect_and_self_test(self):
        self.assertEqual(self.parse('detect').cmd, 'detect')
        self.assertEqual(self.parse('self-test').cmd, 'self-test')


class BridgeBehaviour(unittest.TestCase):
    def test_startup_config_exit_code_is_42(self):
        # FIX 2: lib.rs treats exit 42 as a configuration failure and does not restart
        self.assertEqual(pb.RENDER_STARTUP_EXIT, 42)

    def test_heartbeat_payload(self):
        buf = io.BytesIO()
        out = io.TextIOWrapper(buf, encoding='utf-8')
        with redirect_stdout(out):
            try:
                pb.emit_heartbeat('job', done=3)
            finally:
                out.flush()
        lines = [json.loads(l) for l in buf.getvalue().decode('utf-8').splitlines() if l.strip()]
        self.assertEqual(lines[-1]['type'], 'heartbeat')
        self.assertEqual(lines[-1]['done'], 3)

    def test_resident_icustom_path_uses_forward_slashes(self):
        self.assertEqual(pb._resident_indicator_rel('abc', Path('demo.ex5')), 'MQLLibraryPreview/abc/demo')

    def test_runtime_hard_kill_exists(self):
        self.assertTrue(callable(pb.hard_kill))
        self.assertIn('psutil.process_iter', bridge_source())


class BridgeSourceGuards(unittest.TestCase):
    """Static: these live in generated MQL/ini/SQL text or in code paths that need a live MetaTrader."""

    def assertHas(self, *needles):
        src = bridge_source()
        for n in needles:
            self.assertIn(n, src)

    def test_no_live_trading_in_render_terminals(self):  # P0.3
        self.assertNotIn('AllowLiveTrading=1', bridge_source())

    def test_whole_mql_tree_seed_does_not_return(self):
        self.assertNotIn('copytree(src,dst,dirs_exist_ok=True,ignore=ign)', bridge_source())

    def test_runtime_markers_and_selection_cache(self):
        self.assertHas('mt5-preview-prime-v3', 'seed-v5', 'selection-v5.json')

    def test_shutdown_config_is_numeric(self):
        self.assertHas('ShutdownTerminal=1')

    def test_sandbox_failure_message(self):
        self.assertHas('Preview sandbox initialization failed')

    def test_render_diagnostic_stages(self):
        self.assertHas('terminal_selected', 'indicator_staged', 'indicator_compile_success')

    def test_atomic_sha256_dedup_claim(self):
        self.assertHas('BEGIN IMMEDIATE', 'GROUP BY sha256')

    def test_legacy_static_fast_fail_rows_are_recovered(self):
        self.assertHas("preview_error LIKE 'MT4 source in .mq5:%'")

    def test_resident_ea_file_protocol(self):
        self.assertHas('RESIDENT_EA_SOURCE')
        for n in ('current.job', 'current.done', 'heartbeat.txt'):
            self.assertIn(n, pb.RESIDENT_EA_SOURCE)

    def test_resident_ea_waits_for_calculated_buffers(self):  # overlay fix
        for n in ('if(bc>0)', 'stable>=5', 'for(int i=0;i<60;i++)',
                  'CopyRates(_Symbol,_Period,0,3000,rr)', 'ChartNavigate(0,CHART_END,0)', '__MQLLIB_EMPTY_CHART__'):
            self.assertIn(n, pb.RESIDENT_EA_SOURCE)

    def test_hang_timeout_clamped_to_40s(self):
        self.assertHas('hang_timeout=max(40')

    def test_logged_in_render_seed(self):  # FIX 1
        self.assertHas('def resolve_mt5_render_data_dir', 'def seed_render_runtime_from_data_dir')

    def test_first_heartbeat_handshake(self):  # FIX 2
        self.assertHas('def _wait_for_resident_startup', 'resident_ea_ready', 'relaunch_cap=3')

    def test_compile_pool_targets_shared_render_runtime(self):  # 4802 fix
        self.assertHas("runtime_path(sel,dest,'MT5','render-server')", '_wait_for_render_runtime',
                       'render_artifact_missing', 'ex5 missing in render runtime')

    def test_render_dependencies_and_common_folder_seeded(self):  # residual 4802 fix
        self.assertHas('def seed_render_dependencies_from_data_dir',
                       "for name in ('Indicators','Include','Libraries','Files')",
                       "common_src=real_data.parent/'Common'")

    def test_metaeditor_include_root_and_oninit_classification(self):  # residual 4802 fix
        self.assertHas("f'/inc:{mqlroot}'", 'indicator OnInit failed (err 4802)')

    def test_preview_selftest_runs_the_real_pipeline(self):
        self.assertHas('def preview_selftest', 'selftest_report.json', 'selftest_contact_sheet.png',
                       "pipeline':'compile-pool+resident-ea", 'compile_pool(', 'render_server(')


class AppContracts(unittest.TestCase):
    """The UI and the Rust supervisor agree with the bridge."""

    def test_versions_match(self):
        pkg = json.loads(_read('package.json'))['version']
        conf = json.loads(_read('src-tauri/tauri.conf.json'))['version']
        import re
        cargo = re.search(r'(?m)^version\s*=\s*"([^"]+)"', _read('src-tauri/Cargo.toml')).group(1)
        self.assertRegex(pkg, r'^\d+\.\d+\.\d+$')
        self.assertEqual((conf, cargo), ('../package.json', pkg))

    def test_psutil_is_a_build_dependency(self):
        self.assertRegex(_read('requirements-build.txt'), r'(?m)^psutil')

    def test_rust_supervisor(self):
        lib = _read('src-tauri/src/lib.rs')
        for n in ('preview_library_heartbeat_timeout', 'preview_library_restart', 'run_preview_worker_supervisor',
                  'run_compile_pool_process', '"render_server":1', '"compile_workers"', 'final_code==42',
                  'preview-library-config-error', '"--runtime-id"', 'render_args.push'):
            self.assertIn(n, lib)

    def test_main_js(self):
        main = _read('ui/main.js')
        for n in ('preview.workers', 'defaultPreviewWorkers', "'render-server'", 'mt5DataDir', 'preview.mt5DataDir',
                  'preview-library-config-error', 'render_startup_config', 'Run preview self-test', "'preview-selftest'",
                  'previewSelfTestSheet', '40,40,180', "Can't preview (indicator won't initialize)"):
            self.assertIn(n, main)
        self.assertNotIn("'render-library','--db'", main)  # only the resident render-server path

    def test_preview_ui_js(self):
        ui = _read('ui/preview_ui.js')
        for n in ('Clear Application / Start Fresh', 'existing?.dataset.source===source', 'queueCancellation',
                  'preview_cancel_result', 'ui_event_loop_stall', 'mt5_full_recompile', 'terminal_selected',
                  "Can't preview (indicator won't initialize)"):
            self.assertIn(n, ui)
        self.assertNotIn('Create Archive Backup', ui)
        self.assertNotRegex(ui, r'\bconfirm\s*\(')  # no native confirm dialog


if __name__ == '__main__':
    unittest.main()
