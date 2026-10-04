# 词库来源

运行 `./rdp-auth wordlist --download` 下载公开数据并构建本机词库。原始游戏资源与生成的词库不随仓库分发，分别位于被 Git 忽略的 `.cache/wordlists/` 和 `wordlists/objects.json`。

- 美食：[THUOCL](https://github.com/thunlp/THUOCL) 饮食分类。上游允许个人、研究与商业使用，具体条件以上游说明为准。引用：韩世依等，THUOCL：清华大学开放中文词库，2016。
- Minecraft：[minecraft-assets](https://github.com/InventivetalentDev/minecraft-assets) 的 1.21.1 简体中文资源，只提取方块、物品、实体与附魔名称。
- 原神：[genshin-db](https://github.com/theBowja/genshin-db) 公开 API，只提取中文角色、武器、材料、料理与圣遗物名称。

下载地址、分类和 SHA-256 校验值见 `sources.json`。原神 API 会更新；审核来源后可使用 `--download --refresh-sources` 更新快照。只保留至少两个汉字的名称，部分外层引号会移除，因此不是上游完整数据集。

本项目的 MIT 许可仅适用于项目代码，不改变第三方词库、游戏内容、商标或其他权利的归属。请在发布自行生成的数据集时核对对应上游许可。
