const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const net = require("node:net");
const { PassThrough } = require("node:stream");
const { test } = require("node:test");
const { assertInstrumentation, assertPortAvailable, collectRuntimeDiagnostics, launchEnvironment, observeProcess, readStartupState, waitUntil, withTimeout } = require("./release-portable-e2e.cjs");

function child() {
  return Object.assign(new EventEmitter(), { pid: 1234, stdout: new PassThrough(), stderr: new PassThrough() });
}

test("startup diagnostics retain bounded stdout and stderr", () => {
  const app = child();
  const monitor = observeProcess(app, 8);
  app.emit("spawn");
  app.stdout.write("0123456789");
  app.stderr.write("error-output");
  assert.equal(monitor.state.status, "running");
  assert.equal(monitor.state.stdout, "23456789");
  assert.equal(monitor.state.stderr, "r-output");
});

test("spawn errors fail immediately instead of timing out", async () => {
  const app = child();
  const monitor = observeProcess(app);
  app.emit("error", new Error("spawn ENOENT"));
  let checked = false;
  await assert.rejects(waitUntil(() => { checked = true; }, "CDP", 30000, monitor), /failed to start: spawn ENOENT/);
  assert.equal(checked, false);
});

test("early app exit reports both decimal and Windows exception code", async () => {
  const app = child();
  const monitor = observeProcess(app);
  app.emit("exit", -1073741515, null);
  await assert.rejects(waitUntil(() => true, "CDP", 30000, monitor), /code=-1073741515 \(0xC0000135\)/);
});

test("app exit during a CDP probe is not swallowed or mistaken for success", async () => {
  const app = child();
  const monitor = observeProcess(app);
  await assert.rejects(waitUntil(() => { app.emit("exit", 0, null); return true; }, "CDP", 30000, monitor), /exited before E2E completed/);
});

test("polling retries transient errors and retains last timeout reason", async () => {
  let attempts = 0;
  assert.equal(await waitUntil(() => { if (++attempts === 1) throw new Error("not ready"); return "ready"; }, "CDP", 1000), "ready");
  await assert.rejects(waitUntil(() => { throw new Error("ECONNREFUSED"); }, "CDP", 5), /timeout: CDP; last error: ECONNREFUSED/);
});

test("occupied CDP ports fail before connecting to another application", async () => {
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  try { await assert.rejects(assertPortAvailable(port), /CDP port .* is unavailable/); }
  finally { await new Promise((resolve) => server.close(resolve)); }
  await assertPortAvailable(port);
});

test("runtime diagnostics query elevation and installed WebView2 without mutation", () => {
  let called = false;
  const runtime = collectRuntimeDiagnostics("win32", (exe, args, options) => {
    called = true;
    assert.equal(exe, "powershell.exe");
    const script = args.at(-1);
    assert.match(script, /WindowsBuiltInRole/);
    assert.match(script, /Get-ItemProperty/);
    assert.match(script, /msedgewebview2\.exe/);
    assert.doesNotMatch(script, /Set-Item|New-Item|Remove-Item|ExecutionPolicy|Get-ChildItem Env/);
    assert.equal(options.timeout, 10000);
    return { status: 0, stdout: '\uFEFF{"elevated":true,"installedRuntimes":[{"version":"150.0.4078.65"}],"runtimeExecutables":[]}' };
  });
  assert.equal(called, true);
  assert.equal(runtime.elevated, true);
  assert.equal(runtime.installedRuntimes[0].version, "150.0.4078.65");
});

test("runtime probe failures remain visible and non-Windows skips PowerShell", () => {
  assert.match(collectRuntimeDiagnostics("win32", () => ({ status: 1, stderr: "registry probe failed" })).probeError, /registry probe failed/);
  assert.match(collectRuntimeDiagnostics("win32", () => ({ status: 0, stdout: "bad JSON" })).probeError, /Cannot parse/);
  assert.equal(collectRuntimeDiagnostics("linux", () => assert.fail("must not spawn")).platform, "linux");
});

test("launch environment omits signing secrets and smoke cannot inherit CDP", () => {
  const base = { PATH: "normal", TAURI_SIGNING_PRIVATE_KEY: "secret", TAURI_SIGNING_PRIVATE_KEY_PASSWORD: "password", RELEASE_SIGNING_KEY: "secret", release_signing_key_password: "password", WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: "--remote-debugging-port=9876" };
  const smoke = launchEnvironment(base, false);
  assert.equal(smoke.PATH, "normal");
  assert.equal(Object.keys(smoke).some((key) => /SIGNING|ADDITIONAL_BROWSER_ARGUMENTS/i.test(key)), false);
  assert.equal(base.TAURI_SIGNING_PRIVATE_KEY, "secret");
  assert.equal(launchEnvironment(base).WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS, "--remote-debugging-port=9227");
});

test("production startup smoke requires the launched PID's own window and WebView2 child", () => {
  const state = readStartupState(1234, (exe, args) => {
    const script = args.at(-1);
    assert.match(script, /Get-Process -Id 1234/);
    assert.match(script, /ParentProcessId -eq 1234/);
    assert.match(script, /MainWindowHandle/);
    assert.doesNotMatch(script, /CommandLine|Set-Item|New-Item|Remove-Item/);
    return { status: 0, stdout: '{"mainWindowHandle":42,"webviewProcesses":[{"pid":5678,"parentPid":1234}]}' };
  });
  assert.equal(state.mainWindowHandle, 42);
  assert.equal(state.webviewProcesses[0].parentPid, 1234);
  assert.throws(() => readStartupState("1234; malicious"), /Invalid.*PID/);
});

test("instrumentation cannot be mislabeled as production or used for release tags", () => {
  const env = { CAD_STUDIO_E2E_CDP_CONFIGURED: "1", CAD_STUDIO_UPDATER_SIGNING_MODE: "ci-test", GITHUB_ACTIONS: "true", GITHUB_REF: "refs/heads/main" };
  assert.doesNotThrow(() => assertInstrumentation(env, true, "ipc"));
  assert.doesNotThrow(() => assertInstrumentation({}, false, "startup-smoke"));
  assert.throws(() => assertInstrumentation(env, true, "startup-smoke"), /non-instrumented/);
  assert.throws(() => assertInstrumentation(env, false, "ipc"), /do not match/);
  assert.throws(() => assertInstrumentation({}, true, "ipc"), /do not match/);
  for (const changes of [{ GITHUB_ACTIONS: "false" }, { GITHUB_REF: "refs/tags/v0.3.4" }, { CAD_STUDIO_UPDATER_SIGNING_MODE: "production" }]) {
    assert.throws(() => assertInstrumentation({ ...env, ...changes }, true, "ipc"), /non-release GitHub CI/);
  }
});

test("hung IPC calls time out and successful or rejected calls settle normally", async () => {
  await assert.rejects(withTimeout(new Promise(() => {}), "Tauri IPC runtime_health", 5), /timeout: Tauri IPC runtime_health/);
  assert.equal(await withTimeout(Promise.resolve("ready"), "Tauri IPC", 1000), "ready");
  await assert.rejects(withTimeout(Promise.reject(new Error("IPC failed")), "Tauri IPC", 1000), /IPC failed/);
});
