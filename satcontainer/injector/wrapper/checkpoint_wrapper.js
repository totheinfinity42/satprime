#!/usr/bin/env node
'use strict';

/**
 * SatContainer Node.js Checkpoint Wrapper
 *
 * 自动分析目标 JS 脚本的 require() / import 语句，预加载依赖后发出检查点信号。
 * 通过在同一进程内 require() 执行脚本来保留预加载的模块缓存。
 *
 * 环境变量:
 *   CHECKPOINT_ENABLED: "1" 启用检查点模式（阻塞等待 SIGUSR1）
 *   ORIGINAL_ENTRYPOINT : JSON 格式的原始 ENTRYPOINT
 *   ORIGINAL_CMD        : JSON 格式的原始 CMD
 *   CHECKPOINT_READY_FILE: ready 标记文件路径（默认 /tmp/checkpoint_ready）
 */

const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

// ---- 配置 ----
const LOG_PREFIX = '[SatContainer]';
const CHECKPOINT_ENABLED = process.env.CHECKPOINT_ENABLED === '1';
const CHECKPOINT_READY_FILE = process.env.CHECKPOINT_READY_FILE || '/tmp/checkpoint_ready';
const builtinModules = (() => {
    try { return require('module').builtinModules; } catch (_) { return []; }
})();

// ---- 工具函数 ----

function timestamp() {
    const d = new Date();
    return d.toISOString().split('T')[1].replace('Z', '');
}

function log(msg) {
    console.log(`[${timestamp()}] ${LOG_PREFIX} ${msg}`);
}

function logError(msg) {
    console.error(`[${timestamp()}] ${LOG_PREFIX} ERROR: ${msg}`);
}

// ---- 依赖提取 (零外部依赖，纯正则) ----

function extractRequires(source) {
    var modules = new Set();
    var re = /require\s*\(\s*['"]([^'"]+)['"]\s*\)/g;
    var m;
    while ((m = re.exec(source)) !== null) {
        if (!m[1].startsWith('.') && !m[1].startsWith('/')) {
            modules.add(m[1]);
        }
    }
    return modules;
}

function extractStaticImports(source) {
    var modules = new Set();
    // import xxx from 'yyy', import {a,b} from 'yyy', import * as x from 'yyy'
    var re = /from\s+['"]([^'"]+)['"]/g;
    var m;
    while ((m = re.exec(source)) !== null) {
        if (!m[1].startsWith('.') && !m[1].startsWith('/')) {
            modules.add(m[1]);
        }
    }
    // import 'yyy'
    re = /import\s+['"]([^'"]+)['"]/g;
    while ((m = re.exec(source)) !== null) {
        if (!m[1].startsWith('.') && !m[1].startsWith('/')) {
            modules.add(m[1]);
        }
    }
    return modules;
}

function extractDynamicImports(source) {
    var modules = new Set();
    var re = /import\s*\(\s*['"]([^'"]+)['"]\s*\)/g;
    var m;
    while ((m = re.exec(source)) !== null) {
        if (!m[1].startsWith('.') && !m[1].startsWith('/')) {
            modules.add(m[1]);
        }
    }
    return modules;
}

function isESM(source, filename) {
    if (filename.endsWith('.mjs')) return true;
    if (filename.endsWith('.cjs')) return false;
    return /(?:^|\n)\s*(?:import\s+|export\s+(?:default\s+|const\s+|function\s+|class\s+|async\s+function\s+|\{))/m.test(source);
}

// ---- 脚本查找 ----

function findJsScript(args) {
    for (var i = 0; i < args.length; i++) {
        var arg = args[i];
        // 跳过 node / nodejs 本身
        if (arg === 'node' || arg === 'nodejs') continue;
        // 跳过 node 的 flag 参数
        if (arg.startsWith('-')) continue;
        if (arg.endsWith('.js') || arg.endsWith('.mjs') || arg.endsWith('.cjs')) {
            return arg;
        }
    }
    return null;
}

// ---- 检查点暂停 ----

function waitForSigusr1() {
    return new Promise(function (resolve) {
        var keepAlive = setInterval(function () {}, 1000000);

        function onSigusr1() {
            clearInterval(keepAlive);
            process.removeListener('SIGUSR1', onSigusr1);
            try { fs.unlinkSync(CHECKPOINT_READY_FILE); } catch (_) {}
            log('Received SIGUSR1, continuing...');
            resolve();
        }

        process.on('SIGUSR1', onSigusr1);

        try {
            fs.writeFileSync(CHECKPOINT_READY_FILE, String(process.pid));
        } catch (e) {
            logError('Failed to create ready file: ' + e.message);
        }

        log('Waiting for SIGUSR1 signal (PID: ' + process.pid + ')...');
    });
}

// ---- 模块预加载 ----

function isBuiltin(name) {
    // 取顶层包名，因为 require 只能检查顶层
    var top = name.split('/')[0];
    if (name.startsWith('@')) {
        top = name.split('/').slice(0, 2).join('/');
    }
    return builtinModules.indexOf(top) >= 0;
}

function tryRequire(name) {
    try {
        require(name);
        return true;
    } catch (e) {
        if (e.code === 'ERR_REQUIRE_ESM') {
            return false; // ESM-only, caller should use import()
        }
        throw e;
    }
}

async function preloadModule(name) {
    if (isBuiltin(name)) {
        log("Module '" + name + "' is built-in, skipping");
        return;
    }

    var start = Date.now();

    // 先尝试 CommonJS require
    var loaded = false;
    try {
        loaded = tryRequire(name);
    } catch (e) {
        log("Failed to require '" + name + "': " + e.message + " (skipping)");
        return;
    }

    if (loaded) {
        var elapsed = ((Date.now() - start) / 1000).toFixed(2);
        log("Loaded '" + name + "' in " + elapsed + "s");
    } else {
        // ESM-only 包，用动态 import
        try {
            await import(name);
            var elapsed = ((Date.now() - start) / 1000).toFixed(2);
            log("Loaded '" + name + "' (ESM) in " + elapsed + "s");
        } catch (e2) {
            log("Failed to import '" + name + "': " + e2.message + " (skipping)");
        }
    }
}

// ---- 主流程 ----

async function main() {
    var extraArgs = process.argv.slice(2);

    var originalEntrypoint = [];
    var originalCmd = [];
    try {
        originalEntrypoint = JSON.parse(process.env.ORIGINAL_ENTRYPOINT || '[]');
        originalCmd = JSON.parse(process.env.ORIGINAL_CMD || '[]');
    } catch (e) {
        logError('Failed to parse ORIGINAL_ENTRYPOINT/ORIGINAL_CMD: ' + e.message);
    }

    log('Checkpoint wrapper starting...');
    log('Original ENTRYPOINT: ' + JSON.stringify(originalEntrypoint));
    log('Original CMD: ' + JSON.stringify(originalCmd));
    log('Extra args: ' + JSON.stringify(extraArgs));
    log('Checkpoint enabled: ' + CHECKPOINT_ENABLED);

    // 构建完整命令
    var fullCmd = originalEntrypoint.concat(
        extraArgs.length > 0 ? extraArgs : originalCmd
    );

    log('Full command: ' + JSON.stringify(fullCmd));

    // 找到 JS 脚本
    var scriptPath = findJsScript(fullCmd);
    if (!scriptPath) {
        logError('No JS script found in command');
        process.exit(1);
    }

    var absScriptPath = path.resolve(scriptPath);
    if (!fs.existsSync(absScriptPath)) {
        logError('Script not found: ' + absScriptPath);
        process.exit(1);
    }

    log('Found JS script: ' + absScriptPath);

    // 脚本目录加入模块搜索路径，确保本地模块可解析
    var scriptDir = path.dirname(absScriptPath);
    if (module.paths.indexOf(scriptDir) === -1) {
        module.paths.unshift(scriptDir);
    }
    var localNodeModules = path.join(scriptDir, 'node_modules');
    if (module.paths.indexOf(localNodeModules) === -1) {
        module.paths.unshift(localNodeModules);
    }
    log('Added script dir to module paths: ' + scriptDir);

    // 读取并分析脚本
    var source;
    try {
        source = fs.readFileSync(absScriptPath, 'utf-8');
    } catch (e) {
        logError('Failed to read script: ' + e.message);
        process.exit(1);
    }

    var modules = new Set();
    extractRequires(source).forEach(function (m) { modules.add(m); });
    extractStaticImports(source).forEach(function (m) { modules.add(m); });
    extractDynamicImports(source).forEach(function (m) { modules.add(m); });

    var moduleList = Array.from(modules).sort();

    if (moduleList.length > 0) {
        log('Found ' + moduleList.length + ' modules to preload: ' + JSON.stringify(moduleList));

        var startTime = Date.now();
        var loadedCount = 0;

        for (var i = 0; i < moduleList.length; i++) {
            await preloadModule(moduleList[i]);
            loadedCount++;
        }

        var totalTime = ((Date.now() - startTime) / 1000).toFixed(2);
        log('Preloaded ' + loadedCount + ' modules in ' + totalTime + 's');
    } else {
        log('No external modules found to preload');
    }

    // 检查点暂停
    if (CHECKPOINT_ENABLED) {
        await waitForSigusr1();

        // 恢复后重新读取配置文件（支持 restore 时修改参数）
        // 预留：可在此重新解析 run.json 覆盖参数
    }

    // 构建目标脚本参数
    var scriptIndex = fullCmd.indexOf(scriptPath);
    var scriptArgs = scriptIndex >= 0 ? fullCmd.slice(scriptIndex + 1) : [];

    // 转发到原始应用
    var isEsm = isESM(source, absScriptPath);
    process.argv = ['node', absScriptPath].concat(scriptArgs);

    if (isEsm) {
        log('Running script as ESM: ' + absScriptPath + ' ' + scriptArgs.join(' '));
        // 确保即使之前被缓存也能重新执行
        await import(pathToFileURL(absScriptPath).href + '?t=' + Date.now());
    } else {
        log('Running script in-process: ' + absScriptPath + ' ' + scriptArgs.join(' '));
        // 清除可能的缓存，确保脚本重新执行
        var resolved = require.resolve(absScriptPath);
        delete require.cache[resolved];
        require(absScriptPath);
    }
}

main().catch(function (err) {
    logError(err.stack || err.message);
    process.exit(1);
});
