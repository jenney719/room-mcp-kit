# room-mcp-kit

`room-mcp-kit` 是一个可自托管的像素房间与 MCP 工具包：使用者通过网页控制一个角色，AI 通过 MCP 控制另一个角色，两边共享同一个房间状态。

## 当前状态

当前版本包含可独立运行的 Room 前端、共享状态服务、MCP 服务、素材校准页与资源处理工具。

- 宽屏 Room 前端支持镜头移动、缩放、人物移动、家具交互和建造布局保存。
- REST 与 MCP 共享同一份房间状态，公共角色 ID 为 `owner` / `companion`。
- MCP 默认返回精简人物状态与操作回执，家具、事件和语义地图按需请求，减少模型上下文消耗。
- 仓库自带中性几何示例角色、背景与家具，克隆后无需准备人物素材即可预览。
- 校准页、人物与家具处理脚本、扩展教程和 CI workflow 均包含在仓库中。

扩展人物、家具或场景前，可以先阅读 [Room 扩展教程](docs/room-tutorials/README.md)。项目的公开内容边界记录在 [开源导出清单](docs/OPEN_SOURCE_EXPORT_CHECKLIST.md) 中。

## 本地预览

不要直接双击 HTML 使用 `file://` 打开。安装依赖后，从仓库根目录运行：

```powershell
python -m pip install -r room_service/requirements.txt
python tools/run_room_shared_dev.py --port 8877
```

然后访问：

- Room：`http://127.0.0.1:8877/`
- 校准页：`http://127.0.0.1:8877/calibration.html`

## 公共角色模型

- `owner`：由网页使用者控制。
- `companion`：由 AI 通过 MCP 控制。

公共版保留四个通用 MCP 工具：

- `room_get_state`
- `room_move`
- `room_use_furniture`
- `room_stop`

家具交互统一通过 `room_use_furniture` 完成，不为每件家具增加一个 MCP 工具。

## 远程使用

本 fork 增加了单端口的远程网页登录与 Streamable HTTP MCP 网关。
配置独立的房间密码、MCP 令牌、HTTPS 地址及持久数据库路径后运行
`python -m room_service.remote`，并通过 HTTPS 代理访问。
详情见 [远程部署说明](docs/room-tutorials/09-remote-room.md)。

## 默认人物与自定义人物素材

不准备额外人物素材也可以直接运行。仓库自带 `owner` 和 `companion` 两个匿名几何 SVG 小人，用于演示移动、状态同步和家具交互；它们没有私人 OC 特征，并随代码采用 MIT License。默认示例在不同动作中复用同一张 SVG，因此状态会变化，但不会显示精细的走路、坐下或睡觉姿势。

想换成自己的完整人物时，每个角色、每套服装需要 18 张透明 RGBA PNG：

- idle：上下左右 4 张；
- walk：每个方向 2 张，共 8 张；
- actions：`sit`、`read`、`work-0`、`work-1`、`sleep`、`rest`，共 6 张。

把自己的源图放在被 Git 忽略的 `local-assets/` 或仓库外目录，再使用 `tools/process_character_assets.py` 生成角色包。不要把不准备公开的原图、处理中间图或双人合成人物图提交到公共仓库。完整目录结构和换装流程见 [创建人物与换装教程](docs/room-tutorials/02-characters-and-outfits.md)。

## 许可证

- 代码、配置、文档和中性示例 SVG：MIT，见 [LICENSE](LICENSE)。
- 公开背景、家具和墙饰 PNG：CC BY 4.0，见 [ASSET_LICENSE.md](ASSET_LICENSE.md)。
- OpenAI `gpt-image-2` 素材来源与维护者授权声明：见 [ASSET_PROVENANCE.md](ASSET_PROVENANCE.md)。
- 第三方依赖与 Phaser 通知：见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

使用者自行添加的角色资源不自动获得上述公共资产授权；请按其实际来源与授权单独管理。
