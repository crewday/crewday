"""Scheduler job-body factories.

These modules keep APScheduler registration separate from job-specific
fan-out bodies; import factories through app.worker.scheduler for the
legacy private test seams.
"""
