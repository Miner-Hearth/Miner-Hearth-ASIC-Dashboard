# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for MinerHearth.exe (one file, no console window). Was MinerDashboard.exe up to v1.7.
# Build from the miner-autotune folder:  build-exe.bat   (or: pyinstaller --noconfirm packaging\MinerHearth.spec)
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
PKG = os.path.join(ROOT, "packaging")

a = Analysis(
    [os.path.join(ROOT, "miner_dashboard.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "VERSION"), "."),
        (os.path.join(ROOT, "config.json"), "."),
        (os.path.join(PKG, "miner_dashboard.ico"), "."),
        (os.path.join(ROOT, "assets", "dashboard-bg.jpg"), "assets"),
    ],
    hiddenimports=["autotune", "braiins_api", "dashboard", "scanner", "miner_hearth", "nk_net", "nk_math", "nk_sounds", "nk_ui", "nk_ui19", "nk_coins", "nk_extras", "nk_faults", "nk_fault_codes", "nk_ui110", "nk_ui113", "nk_compat", "nk_adapters", "nk_adapters.base", "nk_adapters.axeos", "nk_adapters.braiins", "nk_adapters.apollo", "nk_adapters.avalon", "nk_adapters.vnish", "nk_adapters.luxos", "nk_adapters.bitmain", "nk_ui114", "nk_ui115", "nk_eventlog", "nk_ui1155", "nk_grace", "nk_poolfeed", "nk_update", "nk_ui116", "nk_pools", "nk_ui1162", "nk_proc", "nk_legacy", "nk_nodes", "nk_ui117", "nk_ui1171", "nk_adapters.whatsminer", "nk_adapters.goldshell", "nk_adapters.elphapex", "nk_adapters.iceriver", "nk_adapters.sealminer", "nk_adapters.generic", "winsound", "webview.platforms.edgechromium", "webview.platforms.winforms"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test", "pydoc_data", "mock_server", "test_autotune", "test_v113", "test_v114", "test_v1154", "test_v1155", "test_v1155_blocks", "test_v1155_chta", "test_v116", "test_v1161", "test_v1162", "test_v1163", "test_v117", "test_v1171"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MinerHearth",
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(PKG, "miner_dashboard.ico"),
    version=os.path.join(PKG, "version_info.txt"),
)
