# Original Code timeline replay

Run the frontend Vite server with an isolated data directory and open
`/tests/fixtures/runtime-diagnostics/index.html`. The fixture uses the real
chat store, timeline and a bounded `data-timeline-scroll-root`. The default
sample is synthetic; this is never a Provider execution or benchmark score.

Optionally place a JSON-encoded string in `captured.json` beside this file,
then choose **Load captured reasoning**. This local generated input is not
committed. Record its length/hash and the source history in verification evidence.

Start streaming, probe interactions, collapse/expand reasoning, and stop the
stream. Metrics include rendered/source characters, React commit duration,
long tasks and timer lag. After stopping, allow the display interval to flush;
rendered text is normalized for whitespace, so compare normalized source if
asserting exact content. A responsive fixture alone does not prove the full
application, real Provider, refresh or history recovery passed.
