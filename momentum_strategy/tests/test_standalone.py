# coding: utf-8
"""单文件版必须与 momentum 包保持同步（由 tools/build_standalone.py 生成）。"""

import ast
import os
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILDER = os.path.join(PROJECT_DIR, 'tools', 'build_standalone.py')
STANDALONE = os.path.join(os.path.dirname(PROJECT_DIR), 'dl_strategy_xtdata.py')


def test_standalone_file_is_up_to_date():
    result = subprocess.run([sys.executable, BUILDER, '--check'],
                            cwd=PROJECT_DIR, capture_output=True, text=True)
    assert result.returncode == 0, (
        '单文件版与 momentum 包不同步，请运行 '
        'python tools/build_standalone.py 重新生成\n' + result.stdout + result.stderr)


def test_standalone_file_parses_and_has_entrypoint():
    with open(STANDALONE, encoding='utf-8') as f:
        source = f.read()

    tree = ast.parse(source)
    names = {node.name for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    for expected in ('main', 'SimAccount', 'VectorBacktestEngine', 'Panel', 'XtDataSource'):
        assert expected in names, f'单文件版缺少 {expected}'

    assert "if __name__ == '__main__':" in source
    # 包内相对 import 必须已经剥掉
    assert 'from .' not in source
