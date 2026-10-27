"""打包入口（PyInstaller）。"""
import multiprocessing
import sys

from mb.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main(sys.argv[1:])
