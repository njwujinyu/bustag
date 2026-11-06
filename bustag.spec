# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置（Windows，onedir，控制台程序）。
# 构建：pyinstaller --noconfirm bustag.spec
# 产物是 dist/bustag/ 目录，双击里面的 bustag.exe 运行；数据放在 exe 旁边的 data/ 目录。
import os

# SPECPATH 是这个 spec 文件所在的目录，也就是仓库根目录
APP_DIR = os.path.join(SPECPATH, 'bustag', 'app')

a = Analysis(
    [os.path.join(APP_DIR, 'index.py')],
    pathex=[SPECPATH],
    binaries=[],
    # 模板和静态文件放在包内的 bustag/app/ 下，和 util.get_resource_dir() 的约定一致
    datas=[
        (os.path.join(APP_DIR, 'views'), 'bustag/app/views'),
        (os.path.join(APP_DIR, 'static'), 'bustag/app/static'),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'pandas', 'IPython', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='bustag',
    icon=os.path.join(APP_DIR, 'static', 'images', 'favicon.ico'),
    console=True,
    debug=False,
    strip=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='bustag',
)
