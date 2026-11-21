# Blast Radius entry

Given a patch to [sqlglot](https://github.com/tobymao/sqlglot) at commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, pick the tests that can fail and split
them across up to 8 workers. [SCORING.md](SCORING.md) has the rules.

## How it decides

For each patch the selector does three things.

1. **Finds the old lines that must execute for the change to matter.**
   [br/pydiff.py](br/pydiff.py) pairs the old and new syntax trees through the
   diff's changed blocks. A change inside a function or lambda body matters only to
   a test that runs that statement. A change to a module or class body takes effect
   at import, so it matters to tests that import the module and can read what the
   statement binds. A comment or formatting change leaves the tree equal and has no
   lines at all.

2. **Looks up which tests execute those lines.** The maps in [maps/](maps) were
   traced from the pinned commit: every test was run alone in a fresh process, with
   `sys.monitoring` recording each line it executed (split by whether a module
   import was in progress), the files it opened, and the classes of the objects
   whose methods it called. A test that never executes an anchor line is not a
   candidate. Data files (the `.sql` fixtures) map to the tests that open them.
   For class-level constants, [br/readers.py](br/readers.py) follows the bound name
   through the source to the lines that read it.

3. **Runs the cheap candidates on the patched tree, and keeps the rest.**
   [br/runner.py](br/runner.py) builds the patched tree as an overlay of symlinks
   in `/tmp`, imports sqlglot from it in a fresh forked worker, and runs candidates
   cheapest first, stopping at the first failure. A candidate that ran and passed
   is dropped; one that was not run is kept, with one exception below. The longest
   tests are never run: CPU seconds cost as much as simulated seconds, and they
   need the most memory.

4. **Lets the two longest tests go when the quick ones show no effect.** Two
   tests cost more than 10 CPU seconds to run (the TPC-DS executor test and
   `test_max_nodes`), and keeping one costs more than everything else on the
   commit. When one is a candidate for a code change, the quick candidates are run
   as evidence (at least five, 0.3 s each at most), then others while the job
   stays within 6 CPU seconds. If nothing fails, the two long ones are dropped
   unrun; anything else not run is kept. This is the only place the selector
   accepts a risk of missing a failure. Measured on 514 practice commits made on
   lines those tests execute: the evidence passed 85 times and a long test then
   failed once (it ran far over its time rather than giving a wrong result).
   `BR_DROP=0` turns the rule off.

Whatever is kept is split longest-first across the worker count that minimises
busiest worker + 2 s per worker.

No table maps a patch, or a kind of edit, to tests. Every map is a fact about the
pinned commit (coverage, file reads, class use, class ancestry), and every decision
to drop a test comes from that coverage and from real runs on the patched code.

## When in doubt it keeps more

- A patch that cannot be parsed or applied keeps every test.
- A Python file whose new text has a syntax error, or whose statement structure
  cannot be paired, widens to the enclosing statement, function or file.
- A change to pytest configuration keeps every test.
- A worker that crashes or overruns leaves its remaining tests kept.
- Any exception in the selector keeps every test for the commits not yet decided.

## Layout

| | |
| --- | --- |
| `selector.py` | Entry point: analysis, the run plan, the worker split |
| `br/patch.py` | Unified diff parser and strict applier |
| `br/pydiff.py` | Old/new syntax tree pairing into anchors |
| `br/readers.py` | Who reads a name bound at module or class level |
| `br/harness.py` | Runs one unittest-style test with pytest's verdict |
| `br/runner.py` | Overlay trees, forked workers |
| `maps/` | Traced from the pinned commit, plain JSON |
| `tools/` | How the maps were built, and the checks used while developing |

## Rebuilding the maps

Inside the image (`docker run -v $PWD:/repo -v $PWD/work:/work -w /app/sqlglot -e SKIP_INTEGRATION=0 <image>`):

```sh
python /repo/tools/timing.py /repo/sample/tests.json /work/timing.json
python /repo/tools/trace_all.py /repo/sample/tests.json /work/trace 10
python /repo/tools/class_tree.py /work/classes.json
python /repo/tools/compile_map.py /repo/sample/tests.json /work/trace /work/timing.json /repo/maps
cp /work/classes.json /repo/maps/classes.json
python /repo/tools/outline.py /app/sqlglot /repo/maps
```

## Trying it

```sh
make sample          # or: sh run_sample.sh
python tools/breakdown.py sample sample/answers.json output.jsonl
```

`tools/mutate.py`, `tools/label.py` and `tools/evaluate.py` make practice commits,
label them by running the whole suite on each, and check that every failing test
is among the selector's candidates.
