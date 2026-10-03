---
name: apple-photos
description: "Use when the user explicitly asks to browse or filter their authorized Apple Photos library on this Mac."
---

# Apple Photos（本机只读）

本 skill 通过照片整理软件共用的本机 PhotoKit 接口读取 Apple「照片」图库。它只返回用户明确授权后查询的图片元数据及缩略图。

## 使用步骤

1. 先运行 `pilot/apple-photos status`。状态为未连接时，说明用户可以在照片整理中心点击“连接 Apple 图库”，或明确要求 skill 连接后再运行 `pilot/apple-photos connect`。不得把读取状态当成连接授权。
2. 连接成功后，仅按用户指定的范围查询；筛选截图可用 `pilot/apple-photos assets --screenshots`，日期范围使用 `--start-date YYYY-MM-DD --end-date YYYY-MM-DD`。
3. 结果按 60 张一页返回。需要下一页时，把上一页 `nextCursor` 原样作为 `--cursor '<JSON>'` 传入。不得连接后自动列出整个图库。
4. 完成后运行 `pilot/apple-photos disconnect`，释放本机会话和内存缓存。

## 隐私和安全边界

- 只在用户明确要求连接时发起 macOS PhotoKit 授权；拒绝后停止并说明系统设置位置，不重试弹窗。
- 不下载 iCloud 资源，不请求或输出原始文件路径。缩略图未在本机时如实说明。
- 不删除、改名、移动、导出或自动清理照片。
- 不把照片、缩略图、资产标识或识别内容发送到云端或第三方服务。
- 不把 Apple 资产当作普通文件夹中的磁盘文件，也不调用文件整理或改名流程。
- 只向用户展示本地查询所需的结果，不在日志、持久索引或共享文档中保存资产标识。
