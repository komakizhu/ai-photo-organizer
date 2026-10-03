# 本地照片整理

这是基于 Immich v3.2.2 网页原型的本地照片整理项目。网页和协调服务放在本仓库；大型模型、前端依赖和构建产物放在 T7_1T APFS。原照片保持在所选目录中。

公开版本不包含本机测试报告、真实照片清单或人脸分析记录。Immich 上游源码保留在 `vendor/immich-v3.2.2/`，其许可文件随源码保留。AI File Sorter 作为独立本地运行程序调用，不复制其源码；使用前请自行确认其版本、许可和接口。

本地状态数据库保存审核决定和索引，不提交到 Git。任何文件改名都必须由网页逐项审核并明确批准；分类、相似度与重复项均为建议，不会触发删除。

Apple「照片」图库通过单独的 PhotoKit 后台组件以只读方式接入。首次使用先运行 `zsh pilot/build_apple_photos_bridge.sh`，再从照片整理中心进入“Apple 图库”并主动连接；连接后仍需点击“加载照片”才查询。组件不下载 iCloud 原图，不提供删除、改名或导出。仓库内的 `skills/apple-photos/SKILL.md` 和 `pilot/apple-photos` CLI 使用同一本机接口。

隔离预览默认使用模拟图库，按钮明确标为“连接模拟图库”。需要真实系统授权时，运行 `python3 pilot/preview_folder_studio.py --apple-photos native`：文件夹仍使用生成样例，Apple 页面改接本机 PhotoKit；启动和打开页面不读取照片，用户点击“连接 Apple 图库”才请求权限。已授权时 macOS 不会重复弹窗；已拒绝时须到系统设置修改权限。

## 公开源码版本

本仓库从已定稿的本地源码导出，不携带本地 Git 历史、真实照片、缩略图缓存、照片索引、账号凭据或私人测试报告。`vendor/` 中保留的图像是上游项目的界面素材和测试资源，不是用户图库。

这是开发原型源码，不是安装包。现有脚本仍包含开发机的外置盘运行目录约定，需要按自己的环境配置。`/Volumes/PhotoSource/Photos` 和 `/path/to/ai-photo-organizer` 是示例路径；请勿直接安装示例 launchd 配置。

### 用生成图片运行隔离预览

先在 `vendor/immich-v3.2.2/web` 按上游依赖要求完成前端构建，然后从仓库根目录运行：

```sh
python3 pilot/immich_selection_preview.py --host 127.0.0.1 --port 4191 --static-root vendor/immich-v3.2.2/web/build/selection-interaction-review/static
```

`--static-root` 应指向实际前端静态构建目录。此预览在内存中生成 108 张 SVG 图片，使用模拟登录，不读取真实图库；不要用于公开网络部署。

运行生成数据接口回归测试：

```sh
python3 -m unittest discover -s pilot -p test_immich_selection_preview.py
```

Immich 的许可保留在 [`vendor/immich-v3.2.2/LICENSE`](vendor/immich-v3.2.2/LICENSE)。
