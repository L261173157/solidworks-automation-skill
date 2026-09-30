const { spawn, spawnSync } = require("child_process");
const net = require("net");
const os = require("os");
const fs = require("fs");
const path = require("path");

const repo = path.resolve(__dirname, "..");
const version = JSON.parse(fs.readFileSync(path.join(repo, "apps", "workbench-ui", "src-tauri", "tauri.conf.json"), "utf8")).version;
const portableRoot = process.env.CAD_STUDIO_PORTABLE_ROOT
  || path.join(repo, "release-output", `CAD-Studio-${version}-Windows-x64`);
const executable = path.join(portableRoot, "CAD Studio.exe");
const isolatedHome = path.join(repo, "release-output", "e2e-home");
const queue = path.join(repo, "release-output", "e2e-queue");
const webviewData = path.join(repo, "release-output", "e2e-webview2");
const releaseOutput = path.resolve(repo, "release-output");
const testMode = process.argv.includes("--preflight") ? "preflight"
  : process.argv.includes("--startup-smoke") ? "startup-smoke" : "ipc";
const diagnosticsPath = path.join(repo, "output", "ci", `portable-e2e-${testMode}.json`);
const cdpPort = 9227;
const cdpEndpoint = `http://127.0.0.1:${cdpPort}`;

/** @brief 限制端到端测试清理范围，避免误删工作区外目录。 */
function assertSafeTestPath(target) {
  const resolved = path.resolve(target);
  if (path.dirname(resolved) !== releaseOutput) throw new Error(`拒绝清理测试目录: ${resolved}`);
}

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

/** @brief 记录当前运行环境，不改动浏览器策略或安全设置。 */
function collectRuntimeDiagnostics(platform = process.platform, run = spawnSync) {
  const result = { platform, architecture: process.arch, osRelease: os.release() };
  if (platform !== "win32") return result;
  const command = String.raw`
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$versions = @()
$client = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
foreach ($key in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$client", "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$client", "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$client")) {
  $entry = Get-ItemProperty -LiteralPath $key -Name pv -ErrorAction SilentlyContinue
  if ($entry -and $entry.pv) { $versions += [ordered]@{ registryPath = $key; version = [string]$entry.pv } }
}
$executables = @()
foreach ($base in @([Environment]::GetFolderPath('ProgramFilesX86'), $env:ProgramFiles, $env:LOCALAPPDATA)) {
  if ($base) {
    $pattern = Join-Path $base 'Microsoft\EdgeWebView\Application\*\msedgewebview2.exe'
    foreach ($file in @(Get-Item -Path $pattern -ErrorAction SilentlyContinue)) {
      $executables += [ordered]@{ path = $file.FullName; version = $file.VersionInfo.FileVersion }
    }
  }
}
[ordered]@{
  elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  installedRuntimes = $versions
  runtimeExecutables = $executables
} | ConvertTo-Json -Depth 4 -Compress
`;
  const probe = run("powershell.exe", ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], {
    encoding: "utf8", windowsHide: true, timeout: 10_000, maxBuffer: 64 * 1024,
  });
  if (probe.error || probe.status !== 0) {
    result.probeError = probe.error?.message || String(probe.stderr || `exit code ${probe.status}`).trim();
    return result;
  }
  try { Object.assign(result, JSON.parse(probe.stdout.replace(/^\uFEFF/, "").trim())); }
  catch (error) { result.probeError = `Cannot parse Windows runtime probe: ${error.message}`; }
  return result;
}

/** @brief 仅把测试需要的调试参数传给被测程序，并排除构建签名密钥。 */
function launchEnvironment(base, debug = true) {
  const env = { ...base };
  for (const key of Object.keys(env)) {
    if (/^(TAURI_SIGNING_PRIVATE_KEY(?:_PASSWORD)?|RELEASE_SIGNING_KEY(?:_PASSWORD)?|WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS)$/i.test(key)) delete env[key];
  }
  Object.assign(env, {
    CODEX_HOME: path.join(isolatedHome, ".codex"),
    CAD_STUDIO_QUEUE_DIR: queue,
    WEBVIEW2_USER_DATA_FOLDER: webviewData,
  });
  if (debug) env.WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = `--remote-debugging-port=${cdpPort}`;
  return env;
}

/** @brief 测试仪器化只能用于明确标记的非发布 CI 构建。 */
function assertInstrumentation(env, hasMarker, mode) {
  const configured = env.CAD_STUDIO_E2E_CDP_CONFIGURED === "1";
  if (mode === "startup-smoke" && (hasMarker || configured)) {
    throw new Error("Production startup smoke requires a non-instrumented portable binary");
  }
  if (hasMarker !== configured) throw new Error("CI E2E instrumentation marker and configuration do not match");
  if (configured && (env.GITHUB_ACTIONS !== "true" || !/^refs\/(heads|pull)\//.test(env.GITHUB_REF || "") || env.CAD_STUDIO_UPDATER_SIGNING_MODE !== "ci-test")) {
    throw new Error("Instrumented E2E requires non-release GitHub CI with test signing");
  }
}

/** @brief 只读确认生产程序窗口和属于该进程的 WebView2 已启动。 */
function readStartupState(pid, run = spawnSync) {
  if (!Number.isSafeInteger(pid) || pid <= 0) throw new Error("Invalid portable application PID");
  const command = String.raw`
$ErrorActionPreference = 'Stop'
$app = Get-Process -Id ${pid} -ErrorAction Stop
$children = @(Get-CimInstance Win32_Process -Filter "Name = 'msedgewebview2.exe'" | Where-Object { $_.ParentProcessId -eq ${pid} })
[ordered]@{
  mainWindowHandle = $app.MainWindowHandle.ToInt64()
  mainWindowTitle = $app.MainWindowTitle
  webviewProcesses = @($children | ForEach-Object { [ordered]@{ pid = $_.ProcessId; parentPid = $_.ParentProcessId; executable = $_.ExecutablePath } })
} | ConvertTo-Json -Depth 4 -Compress
`;
  const result = run("powershell.exe", ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], {
    encoding: "utf8", windowsHide: true, timeout: 5000, maxBuffer: 64 * 1024,
  });
  if (result.error || result.status !== 0) throw new Error(result.error?.message || String(result.stderr || `Startup probe exit ${result.status}`).trim());
  return JSON.parse(result.stdout.replace(/^\uFEFF/, "").trim());
}

/** @brief 提前检测端口占用，防止误连其他应用并把它误判为测试成功。 */
function assertPortAvailable(port = cdpPort) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", (error) => reject(new Error(`CDP port ${port} is unavailable: ${error.message}`)));
    server.listen({ host: "127.0.0.1", port, exclusive: true }, () => server.close(resolve));
  });
}

/** @brief 保存有限的子进程输出并记录启动错误及退出状态。 */
function observeProcess(app, limit = 8192) {
  const state = { pid: app.pid || null, status: "starting", exitCode: null, signal: null, stdout: "", stderr: "" };
  for (const stream of ["stdout", "stderr"]) {
    app[stream]?.on("data", (chunk) => { state[stream] = (state[stream] + chunk.toString()).slice(-limit); });
  }
  app.once("spawn", () => { state.pid = app.pid; state.status = "running"; });
  app.once("error", (error) => { state.status = "failed"; state.error = error.message; });
  app.once("exit", (code, signal) => { state.status = "exited"; state.exitCode = code; state.signal = signal; });
  return {
    state,
    assertRunning() {
      if (state.error) throw new Error(`Portable application failed to start: ${state.error}`);
      if (state.status === "exited") {
        const hex = typeof state.exitCode === "number" ? ` (0x${(state.exitCode >>> 0).toString(16).toUpperCase()})` : "";
        throw new Error(`Portable application exited before E2E completed: code=${state.exitCode}${hex}, signal=${state.signal}`);
      }
    },
  };
}

async function waitUntil(check, label, timeout = 30_000, monitor) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    monitor?.assertRunning();
    try {
      const value = await check();
      monitor?.assertRunning();
      if (value) return value;
    } catch (error) { lastError = error; }
    monitor?.assertRunning();
    await sleep(Math.min(250, Math.max(0, deadline - Date.now())));
  }
  monitor?.assertRunning();
  throw new Error(`timeout: ${label}${lastError ? `; last error: ${lastError.message}` : ""}`);
}

/** @brief IPC 没有内建超时，避免后端卡住导致 CI 无限等待。 */
async function withTimeout(operation, label, timeout) {
  let timer;
  try {
    return await Promise.race([
      operation,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`timeout: ${label}`)), timeout); }),
    ]);
  } finally { clearTimeout(timer); }
}

function writeDiagnostics(diagnostics) {
  fs.mkdirSync(path.dirname(diagnosticsPath), { recursive: true });
  fs.writeFileSync(diagnosticsPath, JSON.stringify(diagnostics, null, 2) + "\n");
}

async function main() {
  const diagnostics = {
    startedAt: new Date().toISOString(), mode: testMode, executable, cdpEndpoint,
    cdpConfiguration: testMode === "startup-smoke" ? "disabled" : process.env.CAD_STUDIO_E2E_CDP_CONFIGURED === "1" ? "CI-only API build configuration" : "environment override",
    webviewProfile: process.env.CAD_STUDIO_E2E_CDP_CONFIGURED === "1" ? "API-relative ci-e2e-only profile" : "environment override (ignored by elevated WebView2 150+)",
    runtime: collectRuntimeDiagnostics(),
  };
  writeDiagnostics(diagnostics);
  console.log(`Portable E2E environment: ${JSON.stringify(diagnostics.runtime)}`);
  if (testMode === "preflight") return;
  let app;
  let browser;
  try {
    diagnostics.stage = "launch prechecks";
    if (!fs.existsSync(executable)) throw new Error(`便携版程序不存在: ${executable}`);
    assertInstrumentation(process.env, fs.existsSync(path.join(portableRoot, "CI-E2E-INSTRUMENTED.txt")), testMode);
    [isolatedHome, queue, webviewData].forEach(assertSafeTestPath);
    fs.rmSync(isolatedHome, { recursive: true, force: true });
    fs.rmSync(queue, { recursive: true, force: true });
    fs.rmSync(webviewData, { recursive: true, force: true });
    fs.mkdirSync(isolatedHome, { recursive: true });
    fs.mkdirSync(queue, { recursive: true });

    if (testMode === "ipc") await assertPortAvailable();
    app = spawn(executable, [], {
      cwd: portableRoot,
      env: launchEnvironment(process.env, testMode === "ipc"),
      stdio: ["ignore", "pipe", "pipe"],
    });
    const monitor = observeProcess(app);
    diagnostics.process = monitor.state;
    if (testMode === "startup-smoke") {
      diagnostics.stage = "production window startup";
      diagnostics.startup = await waitUntil(() => {
        const state = readStartupState(app.pid);
        return state.mainWindowHandle !== 0 && state.webviewProcesses.length > 0 ? state : false;
      }, "production portable main window and WebView2 child", 30_000, monitor);
      // A short survival check catches initialization crashes after creating the window.
      await sleep(1000);
      monitor.assertRunning();
      diagnostics.stage = "complete";
      diagnostics.result = { startupVerified: true, ipcVerified: false };
      console.log(`Production portable startup smoke passed (no IPC verification): ${JSON.stringify(diagnostics.startup)}`);
      return;
    }
    const { chromium } = require("../apps/workbench-ui/node_modules/playwright");
    diagnostics.stage = "CDP startup";
    diagnostics.cdp = await waitUntil(async () => {
      const response = await fetch(`${cdpEndpoint}/json/version`, { signal: AbortSignal.timeout(1000) });
      if (!response.ok) throw new Error(`CDP HTTP status ${response.status}`);
      const version = await response.json();
      if (typeof version.webSocketDebuggerUrl !== "string") throw new Error("CDP browser WebSocket URL is missing");
      return { browser: version.Browser, protocolVersion: version["Protocol-Version"] };
    }, "portable Tauri CDP start", 30_000, monitor);
    browser = await chromium.connectOverCDP(cdpEndpoint, { timeout: 10_000 });
    diagnostics.stage = "Tauri IPC readiness";
    const page = await waitUntil(() => browser.contexts().flatMap((context) => context.pages())[0], "portable Tauri page", 10_000, monitor);
    await page.waitForFunction(() => typeof window.__TAURI_INTERNALS__?.invoke === "function", null, { timeout: 10_000 });
    diagnostics.stage = "portable runtime and worker checks";
    const invoke = (command, args = {}) => withTimeout(page.evaluate(
      ([activeCommand, activeArgs]) => window.__TAURI_INTERNALS__.invoke(activeCommand, activeArgs),
      [command, args],
    ), `Tauri IPC ${command}`, command === "runtime_health" ? 60_000 : 15_000);
    const runtime = await invoke("runtime_health");
    const normalizedRoot = String(runtime.skillRoot || "").replace(/^\\\\\?\\/, "").replace(/\\/g, "/").toLowerCase();
    const expectedRoot = path.join(portableRoot, "skill").replace(/\\/g, "/").toLowerCase();
    if (normalizedRoot !== expectedRoot) {
      throw new Error(`便携版没有使用同目录 skill: actual=${runtime.skillRoot}, expected=${expectedRoot}`);
    }
    for (const required of [
      "SKILL.md",
      "apps/desktop/cad_workbench/queue_worker.py",
      "apps/desktop/cad_workbench/schemas/automation_job.schema.json",
      "examples/08_mini_fan_motion_assembly.py",
      "mcp-server/server.py",
      "mcp-server/register_all_ai_mcp.ps1",
      "subskills/autocad-automation/SKILL.md",
    ]) {
      if (!fs.existsSync(path.join(portableRoot, "skill", ...required.split("/")))) {
        throw new Error(`便携版缺少资源: ${required}`);
      }
    }
    const started = await invoke("start_worker", { repoPath: "", enableCodex: false, codexFullAccess: false });
    if (!started.running || !started.pid) throw new Error(`内置 worker 启动失败: ${JSON.stringify(started)}`);
    const stopped = await invoke("stop_worker");
    if (stopped.running) throw new Error(`worker 未停止: ${JSON.stringify(stopped)}`);
    diagnostics.stage = "complete";
    diagnostics.result = { skillRoot: runtime.skillRoot, python: runtime.python, workerStarted: started, workerStopped: stopped };
    console.log(JSON.stringify(diagnostics.result, null, 2));
  } catch (error) {
    diagnostics.error = error.message;
    if (app?.pid && process.platform === "win32") {
      try { diagnostics.failureStartupState = readStartupState(app.pid); }
      catch (probeError) { diagnostics.failureStartupProbeError = probeError.message; }
    }
    if (diagnostics.stage === "CDP startup" && diagnostics.runtime.elevated) {
      diagnostics.hint = "WebView2 150+ ignores WEBVIEW2_* overrides for elevated hosts. Test using a normal-privilege host or an explicitly test-only build configuration. Do not disable browser security or change production updater trust.";
    }
    console.error(`Portable E2E diagnostics: ${JSON.stringify(diagnostics)}`);
    throw error;
  } finally {
    // Save the failure state before cleanup changes the process exit status.
    diagnostics.finishedAt = new Date().toISOString();
    writeDiagnostics(diagnostics);
    if (browser) await browser.close().catch(() => {});
    if (app?.pid && app.exitCode === null && app.signalCode === null) {
      if (process.platform === "win32") {
        // Clean up only the process tree created by this test, including WebView2.
        const cleanup = spawnSync("taskkill.exe", ["/PID", String(app.pid), "/T", "/F"], { encoding: "utf8", windowsHide: true, timeout: 10_000 });
        if (cleanup.error || cleanup.status !== 0) app.kill("SIGKILL");
      } else app.kill("SIGKILL");
    }
  }
}

module.exports = { assertInstrumentation, assertPortAvailable, collectRuntimeDiagnostics, launchEnvironment, observeProcess, readStartupState, waitUntil, withTimeout };
if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
