"""对话框过滤器回归测试：pywebview 的 parse_file_type 不接受描述里的 "."。"""
from webview.util import parse_file_type

from vpr.api import Api


def test_input_filters_are_valid():
    for f in Api._INPUT_FILTER.split(";;"):
        desc, pattern = parse_file_type(f)
        assert desc
        assert pattern


def test_db_and_qmc_filters_are_valid():
    # 回归：旧值 "KGMusicV3.db (*.db)" 会被 pywebview 拒绝，对话框永远打不开
    for f in Api._DB_FILTER + Api._QMC_KEYDB_FILTER:
        desc, pattern = parse_file_type(f)
        assert desc
        assert pattern
