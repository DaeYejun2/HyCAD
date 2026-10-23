#!/usr/bin/env python3
"""Import captures on Ubuntu without modifying/deleting Prometheus TSDB.

Use --input RAW.jsonl.gz --output CACHE.pkl --replicas name1,name2,... .
Register approved results in the private server catalog afterwards.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hycad_monitor.capture_import import main
if __name__ == '__main__':
    main()
