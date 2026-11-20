#!/usr/bin/env node
/**
 * AstSight CodeGraph embedded worker
 *
 * 以「代码嵌入」方式使用 @colbymchenry/codegraph：本进程直接 import 该库，
 * 在进程内持有 CodeGraph 实例，通过 stdio 上的逐行 JSON 与 Python 侧通信。
 *
 * 与 MCP 的区别（这是本文件存在的理由）：
 *   - MCP 是协议层：工具由 server 定义、面向模型阅读（explore 返回 markdown 包），
 *     调用方无法控制返回结构。
 *   - 本 worker 是库调用层：每个方法的入参与返回结构由 AstSight 自己定义，
 *     输出是结构化 JSON，可直接进确定性管道（C2/C3/C5 事实源环节零 LLM）。
 *
 * 协议（一行一个 JSON）：
 *   请求  {"id": 1, "method": "impact", "params": {...}}
 *   响应  {"id": 1, "ok": true,  "result": {...}}
 *         {"id": 1, "ok": false, "error": {"code": "...", "message": "..."}}
 *
 * 运行要求：Node >= 22.5（库模式依赖内置 node:sqlite）。
 */

import readline from "node:readline";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

/**
 * 定位 @colbymchenry/codegraph。
 *
 * 不在 bridge 目录里再装一份依赖：被测方的 CodeGraph 装在哪由调用方决定，
 * 通过 ASTSIGHT_CODEGRAPH_MODULE_ROOT 传入（该目录下应有 node_modules），
 * 未指定时按「本文件同级 → 向上逐级」的常规 node 解析顺序回退。
 *
 * 注意：该包是 CommonJS，具名 import 会抛
 * SyntaxError: Named export 'CodeGraph' not found，
 * 因此统一用 createRequire 加载再取 CodeGraph。
 */
function loadSdk() {
  const candidates = [];
  if (process.env.ASTSIGHT_CODEGRAPH_MODULE_ROOT) {
    candidates.push(path.resolve(process.env.ASTSIGHT_CODEGRAPH_MODULE_ROOT));
  }
  candidates.push(path.dirname(fileURLToPath(import.meta.url)));

  const errors = [];
  for (const root of candidates) {
    try {
      const req = createRequire(path.join(root, "astsight-bridge.cjs"));
      const raw = req("@colbymchenry/codegraph");
      const sdk = raw?.CodeGraph ? raw : (raw?.default ?? raw);
      if (!sdk?.CodeGraph) {
        throw new Error("module loaded but exposes no CodeGraph export");
      }
      return sdk;
    } catch (error) {
      errors.push(`${root}: ${error?.message ?? error}`);
    }
  }
  throw new Error(
    `cannot load @colbymchenry/codegraph. Tried:\n  ${errors.join("\n  ")}\n` +
      "Set ASTSIGHT_CODEGRAPH_MODULE_ROOT to a directory whose node_modules contains the package.",
  );
}

const { CodeGraph } = loadSdk();

/** root -> CodeGraph 实例。一仓一实例，实例各自持有 .codegraph/ 连接。 */
const instances = new Map();

/** 失败码：调用方据此决定是重试、降级还是报 unsupported。 */
const ERR = {
  UNKNOWN_METHOD: "unknown_method",
  BAD_PARAMS: "bad_params",
  NOT_INDEXED: "not_indexed",
  UNSUPPORTED: "unsupported",
  INTERNAL: "internal",
};

class WorkerError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function requireString(params, key) {
  const value = params?.[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new WorkerError(ERR.BAD_PARAMS, `params.${key} must be a non-empty string`);
  }
  return value;
}

function resolveRoot(params) {
  const root = requireString(params, "root");
  return path.resolve(root);
}

/**
 * 取（必要时创建）某仓库的 CodeGraph 实例。
 *
 * 注意：CodeGraph.init() 不幂等，对已初始化目录会抛
 * "CodeGraph already initialized"。因此必须先用 isInitialized() 探测，
 * 不能用 try { open } catch { init } —— 那样会把 open 的真实故障
 * （文件锁、schema 迁移失败）误判成"未初始化"，掩盖问题。
 */
async function getInstance(params) {
  const root = resolveRoot(params);
  const cached = instances.get(root);
  if (cached) return cached;

  const mode = params?.mode ?? "auto"; // auto | open | init | recreate
  if (mode === "recreate") {
    const cg = await CodeGraph.recreate(root);
    instances.set(root, cg);
    return cg;
  }

  const initialized = CodeGraph.isInitialized(root);
  if (mode === "open" && !initialized) {
    throw new WorkerError(ERR.NOT_INDEXED, `no CodeGraph index at ${root}`);
  }
  if (mode === "init" && initialized) {
    throw new WorkerError(ERR.BAD_PARAMS, `CodeGraph already initialized at ${root}`);
  }

  const readOnly = params?.readOnly === true;
  const cg = initialized
    ? await CodeGraph.open(root, { readOnly })
    : await CodeGraph.init(root);

  instances.set(root, cg);
  return cg;
}

/** 把 CodeGraph 的 Node 对象裁成 AstSight 需要的字段，避免把整个图对象泄进管道。 */
function toSymbolRef(node) {
  if (!node) return null;
  return {
    id: node.id,
    name: node.name,
    qualifiedName: node.qualifiedName ?? node.name,
    kind: node.kind,
    file: node.filePath ?? "",
    line: node.startLine ?? 0,
    endLine: node.endLine ?? 0,
    language: node.language ?? "",
    signature: node.signature ?? "",
    // route 节点把 "GET /api/orders" 塞在 name 里，调用方需要能原样拿到
    raw: node.kind === "route" ? node.name : undefined,
  };
}

function toCallEdge(entry) {
  return {
    symbol: toSymbolRef(entry.node),
    edgeKind: entry.edge?.kind ?? "calls",
  };
}

/**
 * 计算反向闭包实际走到第几层，以及是否在 depth 上限处停下。
 *
 * CodeGraph 的 Subgraph 不返回 depthUsed，旧实现直接回填请求深度，导致
 * 「深度 5 的查询」永远被 Python 侧当成打满（truncated=true），所有
 * 图谱影响都被降成 low/unmapped。这里沿反向边（callee -> caller）做 BFS，
 * 只有确实到达上限且该层还有节点时才标 truncated，宁可保守、不虚报完整。
 */
function impactDepth(edges, roots, maxDepth) {
  const byTarget = new Map();
  for (const edge of edges) {
    if (typeof edge?.source !== "string" || typeof edge?.target !== "string") continue;
    if (!byTarget.has(edge.target)) byTarget.set(edge.target, []);
    byTarget.get(edge.target).push(edge.source);
  }

  const visited = new Set(roots.filter(Boolean));
  let frontier = [...visited];
  let depthUsed = 0;

  while (frontier.length && depthUsed < maxDepth) {
    const next = [];
    for (const node of frontier) {
      for (const caller of byTarget.get(node) ?? []) {
        if (!visited.has(caller)) {
          visited.add(caller);
          next.push(caller);
        }
      }
    }
    if (next.length === 0) break;
    depthUsed += 1;
    frontier = next;
  }

  return {
    depthUsed,
    truncated: depthUsed >= maxDepth && frontier.length > 0,
  };
}

/**
 * 文件路径候选。
 *
 * Node.filePath 的文档口径是「相对项目根」，但调用方可能传 Windows 反斜杠
 * 或绝对路径。这里只在查询层做归一，不把路径修正写进 Python 侧——避免
 * 每个平台各有一套「到底存的是哪种路径」的猜测。
 */
function filePathCandidates(filePath, root) {
  const normalized = filePath.replaceAll("\\", "/");
  const candidates = [filePath, normalized];
  if (!path.isAbsolute(filePath)) {
    const absolute = path.resolve(root, filePath);
    candidates.push(absolute, absolute.replaceAll("\\", "/"));
  }
  return [...new Set(candidates)];
}

const handlers = {
  /** 存活探测，供 Python 侧做启动握手与崩溃重启判断。 */
  async ping() {
    return { pong: true, node: process.version, pid: process.pid };
  },

  /** 建立/复用索引。full=true 走 indexAll，否则走增量 sync。 */
  async "index.ensure"(params) {
    const cg = await getInstance(params);
    const root = resolveRoot(params);
    const alreadyInitialized = CodeGraph.isInitialized(root);
    const full = params?.full === true;

    let result = null;
    if (full) {
      result = await cg.indexAll({});
    } else {
      try {
        result = await cg.sync({});
      } catch (error) {
        // sync 在跨进程写锁被占用时抛 LockUnavailableError。此时索引本身可用，
        // 只是没能增量更新——必须如实上报，不能静默当作已同步。
        if (error?.name === "LockUnavailableError") {
          return {
            indexed: false,
            reason: "lock_unavailable",
            message: error.message,
            stats: cg.getStats(),
          };
        }
        throw error;
      }
    }

    return {
      indexed: true,
      full,
      alreadyInitialized,
      changed: result?.changed ?? null,
      stats: cg.getStats(),
    };
  },

  async stats(params) {
    const cg = await getInstance(params);
    return {
      stats: cg.getStats(),
      frameworks: cg.getDetectedFrameworks(),
      projectRoot: cg.getProjectRoot(),
    };
  },

  async search(params) {
    const cg = await getInstance(params);
    const query = requireString(params, "query");
    const limit = Math.min(Number(params?.limit ?? 10) || 10, 100);
    const kinds = Array.isArray(params?.kinds) ? params.kinds : undefined;
    const matches = cg.searchNodes(query, kinds ? { limit, kinds } : { limit });
    return { matches: matches.map((m) => ({ score: m.score ?? null, symbol: toSymbolRef(m.node) })) };
  },

  /** 精确名查找：枚举同名重载，避免被 FTS 截断。 */
  async "nodes.byname"(params) {
    const cg = await getInstance(params);
    const name = requireString(params, "name");
    return { nodes: cg.getNodesByName(name).map(toSymbolRef) };
  },

  /**
   * 文件内全部节点。
   *
   * C2 用它把 git diff 的行区间映射到符号：这是「不自己实现代码图谱」的
   * 关键——符号边界由 CodeGraph 的提取器给出，AstSight 只做区间相交。
   */
  async "nodes.infile"(params) {
    const cg = await getInstance(params);
    const root = resolveRoot(params);
    const filePath = requireString(params, "filePath");
    for (const candidate of filePathCandidates(filePath, root)) {
      const nodes = cg.getNodesInFile(candidate);
      if (nodes?.length) {
        return { filePath, matchedPath: candidate, nodes: nodes.map(toSymbolRef) };
      }
    }
    return { filePath, matchedPath: null, nodes: [] };
  },

  async callers(params) {
    const cg = await getInstance(params);
    const nodeId = requireString(params, "nodeId");
    const depth = Math.min(Number(params?.depth ?? 1) || 1, 8);
    return { edges: cg.getCallers(nodeId, depth).map(toCallEdge) };
  },

  async callees(params) {
    const cg = await getInstance(params);
    const nodeId = requireString(params, "nodeId");
    const depth = Math.min(Number(params?.depth ?? 1) || 1, 8);
    return { edges: cg.getCallees(nodeId, depth).map(toCallEdge) };
  },

  /**
   * 变更影响半径。
   * 注意粒度：这是符号级，不是接口级。C2 拿到后仍须与路由表求交。
   *
   * 坑：`getImpactRadius` 返回的 `Subgraph.nodes` 是 **Map<string, Node>**，
   * `roots` 是 **node id 字符串数组**。直接 JSON.stringify 会把 Map 变成 `{}`
   * ——数据静默全丢且不报错（样例脚本 reports/library-analysis.json 就是这么
   * 丢的）。这里显式展开 values()，并把 roots 的 id 回查成节点。
   */
  async impact(params) {
    const cg = await getInstance(params);
    const nodeId = requireString(params, "nodeId");
    const depth = Math.min(Number(params?.depth ?? 3) || 3, 8);
    const subgraph = cg.getImpactRadius(nodeId, depth);

    const nodes = subgraph?.nodes instanceof Map
      ? [...subgraph.nodes.values()]
      : Object.values(subgraph?.nodes ?? {});

    const rootIds = Array.isArray(subgraph?.roots) ? subgraph.roots : [];
    const roots = rootIds
      .map((id) => (typeof id === "string" ? cg.getNode(id) : id))
      .filter(Boolean);
    const edges = Array.isArray(subgraph?.edges) ? subgraph.edges : [];
    const { depthUsed, truncated } = impactDepth(edges, rootIds, depth);

    return {
      depthUsed,
      truncated,
      roots: roots.map(toSymbolRef),
      nodes: nodes.map(toSymbolRef),
      edgeCount: edges.length,
      // 图谱自带的召回置信度，'low' 表示只命中了孤立的高频词，不可当完整结果
      confidence: subgraph?.confidence ?? null,
    };
  },

  /**
   * 路由清单。
   *
   * 诚实边界（实测 1.6.2，2026-10-25）：
   *   - 框架检测依赖依赖清单（requirements.txt / pyproject / package.json），
   *     不靠 import 语句推断；目录里没有清单时报 frameworks: []，路由为 0。
   *   - **Flask resolver 只认 `@app.route(..., methods=[...])`**；`@app.get` /
   *     `@app.post` 等 shortcut 一个都不产出（除非项目同时声明 fastapi 依赖，
   *     被 FastAPI resolver 顺带扫到）。纯 Flask 项目全用 shortcut 时路由全丢。
   *   - Flask Blueprint 的 url_prefix、Django include() 的前缀**不会**被拼接，
   *     返回的是去前缀的裸路径；Django 的 HTTP method 完全缺失。
   * 因此本方法只保证「端点存在 + handler 映射」，路径必须由调用方重建。
   * worker 不做修补——修补需要读源码，放在 Python 侧的 route_repair 里做，
   * 这样规则可单测、可审计。
   */
  async routes(params) {
    const cg = await getInstance(params);
    const limit = Math.min(Number(params?.limit ?? 5000) || 5000, 20000);

    const manifest = cg.getRoutingManifest(limit);
    const routeNodes = cg.getNodesByKind("route");

    let entries = (manifest?.entries ?? []).map((e) => ({
      url: e.url,
      handler: e.handler,
      handlerFile: e.handlerFile,
      handlerLine: e.handlerLine,
      handlerKind: e.handlerKind,
      routeId: e.routeId,
      routeFile: e.routeFile,
      routeLine: e.routeLine,
    }));

    /*
     * getRoutingManifest 在有效路由 < 3 条时返回 null（防抖，避免把噪声当路由）。
     * 微服务、单端点服务、样例工程因此会完全拿不到图证据——此时退化为
     * 「route 节点 + 出边 references 反查 handler」。
     *
     * 这一步只补「端点存在 + handler 映射」，路径仍是 CodeGraph 原样输出
     * （前缀照旧缺失），诚实边界不变。
     */
    let fellBackToNodes = false;
    if (entries.length === 0 && routeNodes.length > 0) {
      fellBackToNodes = true;
      entries = [];
      for (const routeNode of routeNodes) {
        const outgoing = cg.getOutgoingEdges(routeNode.id) ?? [];
        let handler = null;
        for (const edge of outgoing) {
          const target = cg.getNode(edge.target);
          if (target && (target.kind === "function" || target.kind === "method")) {
            handler = target;
            break;
          }
        }
        entries.push({
          url: routeNode.name,
          handler: handler?.name ?? "",
          handlerFile: handler?.filePath ?? "",
          handlerLine: handler?.startLine ?? 0,
          handlerKind: handler?.kind ?? "",
          routeId: routeNode.id,
          routeFile: routeNode.filePath,
          routeLine: routeNode.startLine,
        });
      }
    }

    return {
      // getRoutingManifest 在有效路由 < 3 条时返回 null，这不是错误，是防抖策略
      manifestAvailable: manifest !== null,
      fellBackToNodes,
      totalRoutes: manifest?.totalRoutes ?? entries.length,
      entries,
      // 原始 route 节点：manifest 会漏掉 include() 这类非 view 的 route 节点，
      // 两者数量可能不一致，调用方需要能同时看到。
      nodes: routeNodes.map(toSymbolRef),
      frameworks: cg.getDetectedFrameworks(),
    };
  },

  /** 上下文包：给人/Agent 读的 markdown，禁止进确定性判定管道。 */
  async context(params) {
    const cg = await getInstance(params);
    const query = requireString(params, "query");
    const markdown = await cg.buildContext(query, {
      maxNodes: Math.min(Number(params?.maxNodes ?? 20) || 20, 100),
      includeCode: params?.includeCode !== false,
      format: "markdown",
    });
    return { markdown };
  },

  /** 图健康度：C2 判「这次变更要不要跑」时用它做前置闸门。 */
  async "graph.health"(params) {
    const cg = await getInstance(params);
    return {
      cycles: cg.findCircularDependencies(),
      topDependedOn: cg.getTopDependedOn(30).map((item) => ({
        dependents: item.dependents,
        symbol: toSymbolRef(cg.getNode(item.nodeId)),
      })),
      topCallingFiles: cg.getTopCallingFiles(20),
    };
  },

  async close(params) {
    const root = resolveRoot(params);
    const cg = instances.get(root);
    if (cg) {
      cg.close();
      instances.delete(root);
      return { closed: true };
    }
    return { closed: false, reason: "not_open" };
  },
};

async function dispatch(request) {
  const { id, method, params } = request ?? {};
  if (typeof method !== "string") {
    return { id: id ?? null, ok: false, error: { code: ERR.BAD_PARAMS, message: "method required" } };
  }
  const handler = handlers[method];
  if (!handler) {
    return {
      id: id ?? null,
      ok: false,
      error: { code: ERR.UNKNOWN_METHOD, message: `unknown method: ${method}` },
    };
  }
  try {
    const result = await handler(params ?? {});
    return { id: id ?? null, ok: true, result };
  } catch (error) {
    const code = error instanceof WorkerError ? error.code : ERR.INTERNAL;
    return {
      id: id ?? null,
      ok: false,
      error: { code, message: error?.message ?? String(error) },
    };
  }
}

function shutdown() {
  for (const cg of instances.values()) {
    try {
      cg.close();
    } catch {
      // 关停阶段忽略单仓关闭失败，继续释放其余实例
    }
  }
  instances.clear();
  process.exit(0);
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", async (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let request;
  try {
    request = JSON.parse(trimmed);
  } catch {
    process.stdout.write(
      `${JSON.stringify({ id: null, ok: false, error: { code: ERR.BAD_PARAMS, message: "invalid json" } })}\n`,
    );
    return;
  }
  const response = await dispatch(request);
  process.stdout.write(`${JSON.stringify(response)}\n`);
});
rl.on("close", shutdown);

// 握手：让 Python 侧在启动时即可确认 Node 版本满足库模式要求（>= 22.5，
// 库模式依赖 node:sqlite）。22.0~22.4 不满足，因此不能只看大版本号。
const [major, minor] = process.versions.node.split(".").map(Number);
const nodeOk = major > 22 || (major === 22 && minor >= 5);

process.stdout.write(
  `${JSON.stringify({
    id: null,
    ok: true,
    event: "ready",
    node: process.version,
    nodeOk,
  })}\n`,
);
