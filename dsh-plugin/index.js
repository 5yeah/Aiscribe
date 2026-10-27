/**
 * Host half of dsh-aireport.
 *
 * The plugin itself owns no MCP implementation: it resolves the Aiscribe sidecar
 * (PyInstaller build shipped in this package, or a development Python checkout)
 * and mounts the official dsh-mcp-client with a runtime-computed command.
 *
 * Everything path-related is resolved from import.meta.url, so the bundle works
 * from any profile directory and never needs an absolute path in the patch.
 */

import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const platformKey = `${process.platform}-${process.arch}`;
const exeName = process.platform === 'win32' ? 'aireport.exe' : 'aireport';

/** Optional per-platform packages, published only when a platform is supported. */
const PLATFORM_PACKAGES = {
  'win32-x64': 'aireport-win32-x64',
  'darwin-arm64': 'aireport-darwin-arm64',
  'darwin-x64': 'aireport-darwin-x64',
  'linux-x64': 'aireport-linux-x64',
};

function platformBinary() {
  if (process.env.AIREREPORT_BIN) {
    return process.env.AIREREPORT_BIN;
  }

  const bundled = join(here, 'runtime', platformKey, exeName);
  if (existsSync(bundled)) {
    return bundled;
  }

  const pkg = PLATFORM_PACKAGES[platformKey];
  if (pkg) {
    try {
      const manifest = require.resolve(`${pkg}/package.json`);
      const root = dirname(manifest);
      for (const candidate of [join(root, 'bin', exeName), join(root, exeName)]) {
        if (existsSync(candidate)) {
          return candidate;
        }
      }
    } catch {
      // 平台包没发布/没安装：继续尝试开发态 Python
    }
  }
  return null;
}

function pythonCheckout() {
  const configured = process.env.AIREREPORT_REPO;
  const candidates = configured
    ? [resolve(configured)]
    : [dirname(here), here, process.cwd()];

  for (const root of candidates) {
    if (existsSync(join(root, 'ai_report', '__main__.py'))) {
      return root;
    }
  }
  // 只有显式指定了解释器或仓库时，才允许在没有源码的情况下走 Python
  if (process.env.AIREREPORT_PYTHON && configured) {
    return resolve(configured);
  }
  return null;
}

/**
 * @returns {{command: string, args: string[], cwd: string, env: Record<string,string>, kind: string}}
 */
export function resolveLaunch() {
  const binary = platformBinary();
  if (binary) {
    return {
      command: binary,
      args: ['--stdio'],
      cwd: homedir(),
      env: { PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      kind: 'sidecar',
    };
  }

  const root = pythonCheckout();
  if (root) {
    return {
      command: process.env.AIREREPORT_PYTHON || 'python',
      args: ['-m', 'ai_report', 'mcp', '--stdio'],
      cwd: root,
      env: { PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      kind: 'python',
    };
  }

  throw new Error(
    `[aireport] 未找到 Aiscribe sidecar（平台 ${platformKey}）。` +
      '请安装对应平台的 dsh-aireport 版本，或设置 AIREREPORT_BIN 指向 aireport 可执行文件；' +
      '开发态可设置 AIREREPORT_REPO 指向仓库根目录。 / ' +
      `No Aiscribe sidecar for ${platformKey}; set AIREREPORT_BIN or AIREREPORT_REPO.`,
  );
}

export const name = 'aireport';
export const inject = ['tools'];

export async function apply(ctx) {
  const launch = resolveLaunch();
  // 延迟解析 peer：市场扫描/插件列表只需 import 本模块，不要求已安装 MCP 桥
  let applyMcpClient;
  try {
    ({ apply: applyMcpClient } = await import('@deepseek-ai/dsh-mcp-client'));
  } catch (cause) {
    throw new Error(
      '[aireport] 缺少 @deepseek-ai/dsh-mcp-client。请通过 DSH 的插件安装器安装本包，' +
        '或先运行 dsh plugin --profile <name> add dsh-aireport。 / ' +
        'Missing @deepseek-ai/dsh-mcp-client; install this bundle through DSH.',
      { cause },
    );
  }
  ctx.logger.info(`[aireport] starting ${launch.kind} MCP server: ${launch.command}`);
  await applyMcpClient(ctx, {
    transport: 'stdio',
    serverName: 'aireport',
    command: launch.command,
    args: launch.args,
    cwd: launch.cwd,
    env: launch.env,
    toolCallTimeoutMs: 120_000,
    failOnStartupError: true,
  });
}
