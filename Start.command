#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 reset_runner.py doctor
python3 reset_runner.py start
python3 reset_runner.py status
printf '\nSee README.md for run and attach commands. Press Return to close.\n'
read -r
