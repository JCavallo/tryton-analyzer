#!/usr/bin/env bash

GIT_PRE_COMMIT='#!/bin/bash
cd $(git rev-parse --show-toplevel)
poetry run make lint
'

GIT_PRE_PUSH='#!/bin/bash
cd $(git rev-parse --show-toplevel)
poetry run make test
'

if [ -d '.git' ]; then
    echo "$GIT_PRE_COMMIT" > .git/hooks/pre-commit
    echo "$GIT_PRE_PUSH" > .git/hooks/pre-push
    chmod +x .git/hooks/pre-*
fi
