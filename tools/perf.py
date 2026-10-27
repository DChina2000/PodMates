"""性能实测：采样本程序进程（后端+托盘，和面板窗口进程及其 WebView2 子进程）的内存与 CPU。
用法：python tools/perf.py <后端进程PID> <秒数> [标签]"""
import sys
import time

import psutil


def tree(pid):
    p = psutil.Process(pid)
    kids = p.children(recursive=True)
    window = [k for k in kids if k.name().lower() == "podmates.exe"]
    webview = [k for k in kids if "msedgewebview2" in k.name().lower()]
    other = [k for k in kids if k not in window and k not in webview]
    return p, window, webview, other


def main():
    pid, secs = int(sys.argv[1]), float(sys.argv[2])
    label = sys.argv[3] if len(sys.argv) > 3 else ""
    p, window, webview, other = tree(pid)
    groups = {"后端+托盘": [p], "面板窗口进程": window, "WebView2": webview}
    for ps in groups.values():
        for x in ps:
            x.cpu_percent(None)
    samples = {k: [] for k in groups}
    cpu = {k: [] for k in groups}
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(5)
        for k, ps in groups.items():
            mem = cpu_ = 0.0
            for x in ps:
                try:
                    mem += x.memory_info().private / 1048576  # 专用内存（任务管理器「内存」一列）
                    cpu_ += x.cpu_percent(None)
                except psutil.Error:
                    pass
            samples[k].append(mem)
            cpu[k].append(cpu_ / psutil.cpu_count())
    print(f"== {label}（{secs:.0f} 秒，每 5 秒采样；CPU 为占整机的百分比，{psutil.cpu_count()} 个逻辑核）")
    for k in groups:
        if not groups[k]:
            print(f"{k}: 没有运行")
            continue
        m, c = samples[k], cpu[k]
        print(f"{k}: 内存 平均 {sum(m) / len(m):.1f} MB / 最高 {max(m):.1f} MB；CPU 平均 {sum(c) / len(c):.2f}% / 最高 {max(c):.2f}%")


if __name__ == "__main__":
    main()
