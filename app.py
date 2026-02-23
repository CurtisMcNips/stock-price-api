yntaxError: invalid character '─' (U+2500)
  File "/app/.venv/lib/python3.11/site-packages/uvicorn/main.py", line 433, in main
Traceback (most recent call last):
  File "/app/.venv/bin/uvicorn", line 8, in <module>
    sys.exit(main())
             ^^^^^^
  File "/app/.venv/lib/python3.11/site-packages/click/core.py", line 1485, in __call__
    return self.main(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.11/site-packages/click/core.py", line 1406, in main
    rv = self.invoke(ctx)
         ^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.11/site-packages/click/core.py", line 1269, in invoke
    return ctx.invoke(self.callback, **ctx.params)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.11/site-packages/click/core.py", line 824, in invoke
    return callback(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^
    run(
  File "/app/.venv/lib/python3.11/site-packages/uvicorn/main.py", line 606, in run
    server.run()
  File "/app/.venv/lib/python3.11/site-packages/uvicorn/server.py", line 75, in run
    return asyncio_run(self.serve(sockets=sockets), loop_factory=self.config.get_loop_factory())
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.11/site-packages/uvicorn/_compat.py", line 30, in asyncio_run
    return runner.run(main)
           ^^^^^^^^^^^^^^^^
  File "/mise/installs/python/3.11.14/lib/python3.11/asyncio/runners.py", line 118, in run
    return self._loop.run_until_complete(task)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "uvloop/loop.pyx", line 1518, in uvloop.loop.Loop.run_until_complete
  File "/app/.venv/lib/python3.11/site-packages/uvicorn/server.py", line 79, in serve
    await self._serve(sockets)
  File "/app/.venv/lib/python3.11/site-packages/uvicorn/server.py", line 86, in _serve
    config.load()
  File "/app/.venv/lib/python3.11/site-packages/uvicorn/config.py", line 441, in load
    self.loaded_app = import_from_string(self.app)
                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.11/site-packages/uvicorn/importer.py", line 19, in import_from_string
    module = importlib.import_module(module_str)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/mise/installs/python/3.11.14/lib/python3.11/importlib/__init__.py", line 126, in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<frozen importlib._bootstrap>", line 1204, in _gcd_import
  File "<frozen importlib._bootstrap>", line 1176, in _find_and_load
  File "<frozen importlib._bootstrap>", line 1147, in _find_and_load_unlocked
  File "<frozen importlib._bootstrap>", line 690, in _load_unlocked
  File "<frozen importlib._bootstrap_external>", line 936, in exec_module
  File "<frozen importlib._bootstrap_external>", line 1074, in get_code
  File "<frozen importlib._bootstrap_external>", line 1004, in source_to_code
  File "<frozen importlib._bootstrap>", line 241, in _call_with_frames_removed
  File "/app/app.py", line 74
    // ── CURRENCIES ──────────────────────────────────────────────
       ^
SyntaxError: invalid character '─' (U+2500)
