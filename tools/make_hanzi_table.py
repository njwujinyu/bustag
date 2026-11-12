'''
生成 bustag/hanzi.py：繁体字 → 简体字的单字对照表。

只能在 Windows 上运行：对照取自系统自带的 LCMapStringEx（LCMAP_SIMPLIFIED_CHINESE），不用装任何东西。

    python tools/make_hanzi_table.py            重新生成 bustag/hanzi.py
    python tools/make_hanzi_table.py --check    只核对：仓库里的表和这台电脑现在生成的是不是一样

逐个看基本多文种平面里的汉字（CJK 统一汉字的基本区、扩展 A 区和兼容汉字），只收"转换后变成了另一个字"的那些。
日文的新字体（気、学 这一类）系统不当作繁体字，不在表里。
这个脚本只在开发时用，不随程序发布（不在 bustag 包里，exe 和 Docker 镜像里都没有）
'''
import ctypes
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / 'bustag' / 'hanzi.py'
# (起, 止, 叫法)，两头都包括
RANGES = ((0x3400, 0x4DBF, '扩展 A 区'), (0x4E00, 0x9FFF, '基本区'), (0xF900, 0xFAFF, '兼容汉字'))
LCMAP_SIMPLIFIED_CHINESE = 0x02000000
PER_LINE = 40
HEADER = """'''
繁体字 → 简体字的单字对照表：TRADITIONAL 里的第 n 个字对应 SIMPLIFIED 里的第 n 个字，共 {count} 个
（{detail}）。比较文字时把两边都折成简体用，见 bustag.spider.db.fold。

这个文件是 tools/make_hanzi_table.py 生成的，不要手工改；要更新就在 Windows 上重新运行那个脚本。
对照取自 Windows 的 LCMapStringEx（LCMAP_SIMPLIFIED_CHINESE）：只收转换后变成另一个字的汉字，
日文的新字体（気、学 这一类）不在表里
'''
"""


def simplifier():
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    convert = kernel32.LCMapStringEx
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_wchar_p,
                        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    convert.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(8)

    def simplify(char):
        written = convert('zh-CN', LCMAP_SIMPLIFIED_CHINESE, char, 1, buffer, len(buffer), None, None, None)
        if not written:
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer[:written]

    return simplify


def build():
    '''返回 (繁体字的字符串, 对应简体字的字符串, 各个区收了多少个字的说明)'''
    simplify = simplifier()
    pairs = []
    counts = []
    for first, last, name in RANGES:
        found = 0
        for code in range(first, last + 1):
            char = chr(code)
            simple = simplify(char)
            if simple != char and len(simple) == 1:
                pairs.append((char, simple))
                found += 1
        counts.append(f'{name} {found} 个')
    return ''.join(char for char, _ in pairs), ''.join(simple for _, simple in pairs), '，'.join(counts)


def literal(name, text):
    lines = [f"    '{text[start:start + PER_LINE]}'" for start in range(0, len(text), PER_LINE)]
    return f'{name} = (\n' + '\n'.join(lines) + '\n)\n'


def render(traditional, simplified, detail):
    return (HEADER.format(count=len(traditional), detail=detail)
            + literal('TRADITIONAL', traditional) + literal('SIMPLIFIED', simplified))


def main(arguments):
    if sys.platform != 'win32':
        sys.exit('这个脚本要在 Windows 上运行（用的是系统自带的 LCMapStringEx）')
    text = render(*build())
    if '--check' in arguments:
        same = TARGET.exists() and TARGET.read_text(encoding='utf-8') == text
        print('一样' if same else f'不一样：{TARGET} 和这台电脑生成的内容不同')
        return 0 if same else 1
    TARGET.write_text(text, encoding='utf-8', newline='\n')
    print(f'已写入 {TARGET}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
