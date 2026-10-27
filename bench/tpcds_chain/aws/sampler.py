"""Sample this machine once a second for its whole life: CPU busy and waiting on I/O (percent of all cores), and
cumulative network and disk bytes. Runs alongside every job (node.sh starts it); timed runs are lined up with it
afterwards through the start and end times each job logs (runs.tsv).

    python aws/sampler.py OUT.csv
"""

import sys
import time

import psutil

with open(sys.argv[1], "w") as f:
    f.write("t,cpu_busy,cpu_iowait,net_rx,net_tx,disk_read,disk_write\n")
    psutil.cpu_times_percent(None)
    while True:
        time.sleep(1)
        c = psutil.cpu_times_percent(None)
        n, d = psutil.net_io_counters(), psutil.disk_io_counters()
        busy = 100 - c.idle - getattr(c, "iowait", 0)
        f.write(f"{time.time():.2f},{busy:.1f},{getattr(c, 'iowait', 0):.1f},{n.bytes_recv},{n.bytes_sent},"
                f"{d.read_bytes},{d.write_bytes}\n")
        f.flush()
