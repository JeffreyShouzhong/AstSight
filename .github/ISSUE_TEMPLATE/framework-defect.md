---
name: Framework defect
about: 报告 AstSight 框架自身的不足（不是被测系统的 bug）
title: "[framework-defect] "
labels: framework-defect
---

## 环境

- AstSight 版本：
- Python / 操作系统：
- 运行命令：
- 项目类型（HTTP / Dubbo / gRPC / MQ）：

## 现象

<!-- 期望发生什么，实际发生了什么；尽量贴出门禁结论和失败归因。 -->

## 期望

<!-- 你期望框架怎么处理。 -->

## 最小复现步骤

<!-- 命令、配置、必要的输入。 -->

## 诊断包

请用下面的命令生成脱敏诊断包，检查后拖入本 issue：

```bash
astsight feedback collect --run <run_dir>
```

诊断包只包含运行产物，已在本地脱敏（token、认证头、query 值、邮箱/手机号、绝对路径）。
不包含被测系统源码。
