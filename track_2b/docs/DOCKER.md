# Docker 运行与提交

`make run` 启动 **Apertus Mundus、BGE-M3、MXBAI Rerank Base v2、Kev-4B**。
Apertus 仍使用评委提供的 OpenAI 兼容服务。三个辅助模型从镜像内的权重加载，
无需 SiliconFlow、Mixedbread、TypeSafe 或 Hugging Face 的 API Key。

## 一次启动

要求 Docker Engine 28+、Docker Compose 2.24+ 和 GNU Make。宿主机无需安装 Python。
默认 CPU 推理；建议为 Docker 分配至少 32 GB RAM，预留至少 40 GB 磁盘作为构建起点。
这是容量规划建议，最低硬件和端到端延迟须在评测机器上实测。

```sh
cp .env.docker.example .env.docker
# 在 .env.docker 填写评委提供的 LLM_NAME、LLM_BASE_URL、LLM_API_KEY
make run
```

PowerShell 使用 `Copy-Item .env.docker.example .env.docker`。也可直接从运行环境
注入 `LLM_NAME`、`LLM_BASE_URL`、`LLM_API_KEY`，然后执行 `make run`。
没有认证要求的 Apertus 服务可以显式设置空 `LLM_API_KEY`。

打开 <http://localhost:8000/index.html>。第一次构建下载依赖和公开模型权重，可能较久；
后续构建复用 Docker 层缓存。启动会等待三个辅助模型完成加载。
`make stop` 停止容器并保留存档；`make logs` 查看日志。
端口冲突时设置 `APP_PORT=8001`，再运行 `make run`。

`.env.docker` 不提交，也不会发送到镜像构建上下文。辅助服务仅在 Docker 内部网络开放；
宿主机只发布 `127.0.0.1:<APP_PORT>`。本模式提供本机工作台，手机配对继续使用桌面启动方式。

## 本地运行哪些模型

| 服务 | 固定模型 | 用途 |
| --- | --- | --- |
| embedding | `BAAI/bge-m3` | 1024 维稠密向量 |
| reranker | `mixedbread-ai/mxbai-rerank-base-v2` | 候选片段重排 |
| kev | `jaredpalmer/kev-4b` + `Qwen/Qwen3.5-4B-Base` | System One 结构化决策 |
| app | `LLM_NAME` 指定的 Apertus | 叙事、摘要、扩写、Wiki 及决策回退 |

Kev 是独立开源模型，**不是 TypeSafe 托管的 Jev 权重**。默认 `DECISION_PROVIDER=kev`，
不确定或服务错误时直接回退主 Apertus，不额外请求另一个 Apertus 8B 服务。
原有 `apertus`、`apertus-8b`、`jev` 决策模式继续支持。
如有足够 NVIDIA 显存，可使用 `make run-gpu` 为三个辅助服务构建 CUDA 镜像。
这仍连接远端 Apertus；它与另加本地 Apertus 的 `make run-local` 是不同选项。
GPU 版共享 GPU 的总显存必须容纳所有模型和推理缓存，当前 8 GB 显存不足以验收。
兼容既有配置接口，Kev 地址和模型名分别使用 `JEV_URL`、`JEV_MODEL` 字段。

本地 Base v2 与原先云端 `mxbai-rerank-v3.1-listwise` 不同，原有云端评测结果不能
作为本地模型效果。Kev 官方主要验证英文，中文剧情中的决策表现需要额外评估。

三个辅助服务不要求真实密钥。配置中的 `local` 仅用于兼容 OpenAI 客户端的非空 Key
要求，不是购买或注册的凭据。模型版本记录在 `helpers/models.json`，Kev 源码固定到提交
`1f04cfa28e6a708485b28a9585010b9e436b776e`。运行时设置离线模型加载，构建阶段联网。

## 权重和提交大小

2026-10-16 按固定 revision 的公开 Hub 文件元数据核对，原始权重共 **12,716,349,435 字节**，
约 **12.72 GB / 11.84 GiB**：BGE-M3 2.27 GB、Base v2 0.99 GB、Kev 适配器和头
0.135 GB、Qwen 基座 9.32 GB。下载还包含 tokenizer 和配置；镜像另含 PyTorch 等依赖。
15–20 GB 只能作为镜像粗估，实际大小以构建结果为准，多镜像共享层不要重复计算。

```sh
python helpers/model_inventory.py
docker image ls
```

提交的是源码仓库 URL。权重下载在 Docker 构建时完成，不放入 Git 或 `data/`。
`data/` 只包含公开原创演示剧本，远小于 100 MB。

## 数据与设置

命名卷 `app-data` 保存数据库、剧本、导出存档、设置和报告。首次运行自动载入“雾港灯塔”
演示；重启不会覆盖世界。导出存档可在界面另存并再次载入。

配置顺序：进程环境变量 > 持久化设置 > 项目 `.env` > 默认值。同层中 `LLM_*`
优先于对应 `APERTUS_*`，空 Key 也有效。Compose 明确注入的值会在重启时覆盖界面保存值；
修改部署连接应更新 `.env.docker`。持久化设置文件位于 `/app/runtime/settings.env`。

首次演示还会通过本地 BGE-M3 建立向量索引；已有资料更换向量服务时，使用
`POST /api/rag/reindex` 重建，而不是混用服务来源不同的历史向量。

## 可选：Apertus 也在本地运行

```sh
# 先接受 Apertus v1.5 的 Hugging Face 访问条件，并在本机设置 HF_TOKEN
make prepare-model
make run-local
```

这会增加官方 Apertus v1.5 8B GPU 容器；辅助服务仍使用 CPU。要求 NVIDIA Container
Toolkit、匹配的驱动和足够 GPU 显存。默认 16K 上下文、单并发、thinking 关闭。
当前 8 GB GPU 尚未完成该配置验收，不承诺可在 8 GB 显存运行。

`HF_TOKEN` 只进入下载容器，服务从 `model-cache` 缓存读取。可配置
`LOCAL_LLM_IMAGE`、`LOCAL_LLM_NAME`、`LOCAL_MAX_MODEL_LEN`、
`LOCAL_GPU_MEMORY_UTILIZATION`。模型 revision 和官方 amd64 镜像摘要已固定。
此路径实现自主管理部署；完整运行时断网能力仍需单独验证浏览器资源与网络出口。

## 验证

```sh
make smoke       # 临时世界，模拟模型输出，不访问真实模型
make smoke-live  # 临时世界，真实辅助服务和 Apertus，可能消耗 Apertus 配额
```

两者都不修改正在使用的世界。`smoke-live` 先检查向量、重排和 Kev 的真实推理，
再完成摘要、选中分支审核、扩写、提交、重复提交和导出恢复，并启动新进程检查持久化。
`/api/health` 只检查应用和数据库，不调用模型。

## 生成 Track 2b 提交目录

开发机器需要 Python，运行 `make package`。生成 `dist/submission-<UTC时间>/`，
包含仓库根转发 Makefile 和 `track_2b/{src,data,docs,Makefile,technical_report.md}`。
从生成目录根或 `track_2b/` 都可执行 `make run`；`.env.docker` 放在 `track_2b/`。
打包不带私人剧本、数据库、凭据或模型权重，不覆盖已有输出目录，并生成 SHA-256 清单。

正式提交前仍需填写团队信息、实际测试结果，生成不超过六页正文的团队报告 PDF，
补充两分钟内演示视频链接，并通过官方模板创建公开仓库。

来源：[比赛模板](https://github.com/HackApertus/project-template)、
[BGE-M3](https://huggingface.co/BAAI/bge-m3)、
[MXBAI Base v2](https://huggingface.co/mixedbread-ai/mxbai-rerank-base-v2)、
[Kev](https://huggingface.co/jaredpalmer/kev-4b)、
[Kev 源码](https://github.com/jaredpalmer/kev)、
[Apertus](https://huggingface.co/swiss-ai/Apertus-v1.5-8B)。
