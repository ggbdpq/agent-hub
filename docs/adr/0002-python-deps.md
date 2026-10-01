# ADR-0002 Python 依赖管理策略（pnpm 12.4 pypi 实验特性评估）

- 状态：已采纳（2026-09-14）

## 背景

pnpm 12.4 宣布实验性支持在多语言 monorepo 中并发安装 js / crates / pypi 依赖（package.json 耦合 pyproject.toml 与 Cargo.toml）。本项目恰是 js + py 双语言仓库。

## 决策

**MVP 阶段 Python 依赖走 venv + pip（`pnpm api:setup`），不走 pnpm pypi 实验特性。**

## 理由

1. 实验特性（官方自述 experimental）不应作为项目地基：配置 schema 尚未稳定，踩坑成本由学习时间支付。
2. venv + pip + `pyproject.toml`（`pip install -e .[dev]`）是当下最稳的标准做法，且 pyproject.toml 本身已就位——迁移到 pnpm pypi 只是包管理器层面的替换，应用代码零改动。
3. 学习目标是智能体工程模式，不是包管理器先驱。

## 迁移路径（pnpm pypi 稳定后）

1. 根 package.json 声明 pypi 依赖映射（以届时官方文档为准）。
2. 删除 `api:setup` 脚本与 `.venv`；ADR-0002 标记"已取代"。

## 后果

- 正面：依赖解析行为完全可预期；CI 只需 node + python。
- 负面：两条安装命令（pnpm install / api:setup）而非一条。
