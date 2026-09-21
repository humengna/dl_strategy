# coding: utf-8
"""
把 momentum 包打包成单文件脚本 dl_strategy_xtdata.py。

单文件版方便直接丢进 QMT 目录或拷到别的机器上跑，但手工维护两份代码迟早跑偏，
所以这里从包里自动生成：按依赖顺序拼接模块，去掉包内相对 import，
把各模块顶部的第三方 import 合并到文件开头。

    python tools/build_standalone.py                 # 生成到仓库根目录
    python tools/build_standalone.py --check         # 只校验现有文件是否最新
"""

import argparse
import ast
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(HERE, 'momentum')
DEFAULT_OUT = os.path.join(os.path.dirname(HERE), 'dl_strategy_xtdata.py')

# 依赖顺序：被依赖的排前面
MODULES = [
    'progress', 'config', 'indicators', 'panel', 'broker', 'datasource',
    'universe', 'selector', 'timing', 'engine', 'vector_engine', 'report',
    'sample_data', 'diagnostics', 'cli',
]

# 允许重复出现的模块级名字（值相同，合并后无副作用）
ALLOW_DUPLICATE = {'log'}

HEADER = '''# coding: utf-8
"""
A股动量择时策略 [xtdata 单文件版]

热门概念池 + 对数线性回归动量打分 + RSRS修正标准分 + 动量分数连续下降择时
+ 固定 -15% 硬止损。行情走 xtquant.xtdata，交易由内置 SimAccount 模拟撮合。

!! 本文件由 momentum_strategy/tools/build_standalone.py 自动生成，请勿直接修改 !!
   改动请提交到 momentum_strategy/momentum/ 下的模块，再重新生成。

运行
----
  python dl_strategy_xtdata.py --start 20240101 --end 20241231 --cash 200000
  python dl_strategy_xtdata.py --start 20240101 --end 20241231 --download
  python dl_strategy_xtdata.py --check-data
  结果默认保存到 results/bt_<起止日期>_<时间戳>/
"""

'''


def load_module(name):
    path = os.path.join(PACKAGE, f'{name}.py')
    with open(path, encoding='utf-8') as f:
        source = f.read()
    return path, source


def split_module(source):
    """返回 (顶层第三方 import 文本列表, 去掉 import 与 main 守卫后的正文)"""
    tree = ast.parse(source)
    lines = source.splitlines()
    drop = set()
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            # 包内相对 import，合并后不需要
            drop.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset == 0:
            imports.append('\n'.join(lines[node.lineno - 1:(node.end_lineno or node.lineno)]))
            drop.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    for node in tree.body:
        if isinstance(node, ast.If) and ast.dump(node.test).find("'__main__'") >= 0:
            drop.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    body = [line for i, line in enumerate(lines, 1)
            if i not in drop and not line.startswith('# coding')]
    return imports, '\n'.join(body).strip('\n')


def top_level_names(source):
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def build():
    imports = []
    chunks = []
    seen_names = {}

    for name in MODULES:
        path, source = load_module(name)
        module_imports, body = split_module(source)

        for dup in top_level_names(source) & set(seen_names) - ALLOW_DUPLICATE:
            raise SystemExit(
                f'名字冲突: {dup} 同时定义在 {seen_names[dup]}.py 和 {name}.py，'
                f'合并成单文件会相互覆盖，请先改名')
        for new in top_level_names(source):
            seen_names.setdefault(new, name)

        imports.extend(module_imports)
        chunks.append(f'# {"=" * 70}\n# {name}.py\n# {"=" * 70}\n\n{body}\n')

    unique_imports = sorted(set(imports), key=lambda s: (not s.startswith('import'), s))
    parts = [HEADER, '\n'.join(unique_imports), '\n\n']
    parts.append('\n\n'.join(chunks))
    parts.append("\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n")
    return ''.join(parts)


def main():
    parser = argparse.ArgumentParser(description='生成单文件版策略脚本')
    parser.add_argument('--out', default=DEFAULT_OUT, help='输出路径')
    parser.add_argument('--check', action='store_true', help='只检查现有文件是否与包同步')
    args = parser.parse_args()

    content = build()
    ast.parse(content)          # 生成物必须语法正确

    if args.check:
        if not os.path.isfile(args.out):
            print(f'{args.out} 不存在，请先运行 python tools/build_standalone.py')
            return 1
        with open(args.out, encoding='utf-8') as f:
            current = f.read()
        if current != content:
            print(f'{args.out} 与 momentum 包不同步，请重新生成')
            return 1
        print(f'{args.out} 已是最新')
        return 0

    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'已生成 {args.out}（{len(content.splitlines())} 行）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
