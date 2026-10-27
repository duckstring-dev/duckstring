#!/bin/bash
# A short shakedown of single.sh: the mixed scenario once, every engine.
export SF=100 SCENARIOS=mix REPS=1
bash aws/jobs/single.sh
