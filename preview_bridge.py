from __future__ import annotations
import argparse, concurrent.futures, ctypes, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
from mql_text import decode_mql_bytes, read_log_text, read_mql_text
from mt_common import (CREATE_NO_WINDOW, PreviewRuntimeError, RENDER_STARTUP_EXIT, RenderLock, RenderStartupError, SW_HIDE, _same_source, emit, emit_heartbeat, emit_stage, hard_kill, hide_pid, latest_logs, mql_dir_name, read_text, safe_job_id, same_path, startupinfo, terminate_tree)  # noqa: F401
from mt_compile import (compile_file, compile_has_errors, copy_compiled_support, existing_binary, find_live_binary, safe_name)  # noqa: F401
from mt_discovery import (_render_data_activity, _tree_has_file, _valid_mt5_render_data_dir, candidate_install_dirs, choose_runtime, data_activity, decode_origin_bytes, detect, discover_data_dirs, discover_installations, discovery_diagnostics, find_editor, load_runtime_cache, metatrader_data_root, observed_data_folders, pair_runtime_candidates, preview_runtime, read_origin, resolve_mt5_render_data_dir, runtime_cache_path, runtime_candidate_score, selection_checks, source_kind, terminals, write_runtime_cache)  # noqa: F401
from mt_runtime import (_prepare_offline_runtime, _prepare_render_runtime, clone_runtime, copy_mt4_history, copy_mt5_history, prime_mt5_runtime, runtime_path, seed_mql_runtime, seed_render_dependencies_from_data_dir, seed_render_runtime_from_data_dir, stage)  # noqa: F401
from preview_db import (_cache_image_path, _compiled_cache_path, _ensure_preview_queue_schema, _make_thumb, _propagate_existing_ready, _store_preview_image, _thumb_path, _worker_pause_reason, archive_reset, memory_stats, remove_source)  # noqa: F401
from preview_render import (RESIDENT_EA_SOURCE, _capture_resident_baseline, _compile_pool_claim, _install_resident_ea, _launch_resident_terminal, _mql_str, _read_resident_done, _read_resident_heartbeat, _render_failure_message, _render_runtime_ready_marker, _render_server_claim, _render_server_fail, _resident_indicator_rel, _resident_job_paths, _wait_for_render_runtime, _wait_for_resident_startup, compile_pool, mt5_capture_source, open_source, preflight, preview_trace, render, render_mt4, render_mt5, render_server, wait_image)  # noqa: F401
from bridge_selftest import (_selftest_candidates, _selftest_chart_crop, _selftest_contact_sheet, _selftest_copy_png, _selftest_db, _selftest_default_db, _selftest_dominant_color, _selftest_image_metrics, _selftest_label_from_error, _selftest_nonbg_coverage, _selftest_print_summary, _selftest_rank, _selftest_saturated_colors, _selftest_source_text, preview_selftest, self_test, self_test_discovery, self_test_render_dependencies)  # noqa: F401


def build_parser():
    a=argparse.ArgumentParser();s=a.add_subparsers(dest='cmd',required=True)
    s.add_parser('detect');s.add_parser('self-test')
    p=s.add_parser('preflight');p.add_argument('--source',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal')
    p=s.add_parser('render');p.add_argument('--source',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p.add_argument('--job-id')
    p=s.add_parser('render-server');p.add_argument('--db',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p.add_argument('--job-id');p.add_argument('--hang-timeout',type=int,default=40);p.add_argument('--attempts',type=int,default=2);p.add_argument('--mt5-data-dir');p.add_argument('--runtime-id')
    p=s.add_parser('preview-selftest');p.add_argument('--sample',type=int,default=30);p.add_argument('--out',required=True);p.add_argument('--source');p.add_argument('--db');p.add_argument('--terminal');p.add_argument('--workers',type=int,default=4);p.add_argument('--hang-timeout',type=int,default=40);p.add_argument('--min-draw-percent',type=float,default=60.0);p.add_argument('--mt5-data-dir')
    p=s.add_parser('compile-pool');p.add_argument('--db',required=True);p.add_argument('--out',required=True);p.add_argument('--terminal');p.add_argument('--workers',type=int,default=4);p.add_argument('--job-id');p.add_argument('--runtime-id')
    p=s.add_parser('open-source');p.add_argument('--source',required=True)
    p=s.add_parser('memory-stats');p.add_argument('--db',required=True)
    p=s.add_parser('remove-source');p.add_argument('--db',required=True);p.add_argument('--source',required=True)
    p=s.add_parser('archive-reset');p.add_argument('--db',required=True)
    return a

def main():
    x=build_parser().parse_args()
    if x.cmd=='detect':detect()
    elif x.cmd=='self-test':self_test()
    elif x.cmd=='preflight':preflight(x.source,x.out,x.terminal)
    elif x.cmd=='render':render(x.source,x.out,x.terminal,x.job_id)
    elif x.cmd=='render-server':render_server(x.db,x.out,x.terminal,x.job_id,x.hang_timeout,x.attempts,x.mt5_data_dir,x.runtime_id)
    elif x.cmd=='preview-selftest':preview_selftest(x.out,x.sample,x.source,x.db,x.terminal,x.workers,x.hang_timeout,x.min_draw_percent,x.mt5_data_dir)
    elif x.cmd=='compile-pool':compile_pool(x.db,x.out,x.terminal,x.workers,x.job_id,x.runtime_id)
    elif x.cmd=='open-source':open_source(x.source)
    elif x.cmd=='memory-stats':memory_stats(x.db)
    elif x.cmd=='remove-source':remove_source(x.db,x.source)
    elif x.cmd=='archive-reset':archive_reset(x.db)

if __name__=='__main__':
    try:main()
    except PreviewRuntimeError as e:emit({'ok':False,'error':f'{type(e).__name__}: {e}','diagnostics':e.diagnostics});sys.exit(1)
    except Exception as e:emit({'ok':False,'error':f'{type(e).__name__}: {e}'});sys.exit(1)
