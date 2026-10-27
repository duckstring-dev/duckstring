#!/bin/bash
# The first SF100 node: build the data (prep_sf100.sh), then a shakedown of every engine on the mixed scenario.
set -e
bash aws/jobs/prep.sh
bash aws/jobs/sf100_dry.sh
