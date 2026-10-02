# Local handoff briefing

The backup monitor reported that the nightly `photos` sync on host `cobalt`
finished with status `needs_review` at 02:14. The run copied 18,420 files and
reported 3 unreadable source items. The previous successful run was the night
before. No deletion or overwrite was reported. The host is online, and its
last inventory completed at 01:52.

The on-call operator has not yet checked which three source items were
unreadable. The next shift should inspect the operation details, confirm the
three source files are still available, and rerun or verify only after checking
that source data is intact. Keep the run marked for review until those checks
are complete.
