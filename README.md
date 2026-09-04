# AAAC

Adaptive Admission Control core implementation.

## Overview

This repository contains the core library and admission service for the AAAC project, including token handling, event logging, Redis-backed queue, adaptive window, AIMD controller, and FastAPI endpoints.

## Getting Started

```bash
make run MODE=aaac   # start the full system
make test            # run the test suite
```