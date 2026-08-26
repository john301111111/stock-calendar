# 云端方案：电脑不开机也能同步（GitHub 免费版）

把日历文件放到 GitHub 仓库，由 GitHub Actions 每 6 小时在**云端**自动抓取一次数据并更新文件。
你的电脑、Outlook 都不用一直开机；手机和电脑的 Outlook 都订阅同一个网址即可。

```text
GitHub Actions（云端，每 6 小时）
        ↓ 运行 listed_company_calendar.py
GitHub 仓库里的 listed_company_reminders.ics
        ↓ 固定网址
电脑 / 手机 Outlook 自动订阅刷新
```

## 需要你做的（约 5 分钟，免费）

### 1. 注册 GitHub

打开 https://github.com 注册账号（已有账号直接登录）。邮箱验证一下即可，不需要付费。

### 2. 新建一个公开仓库

右上角 `+` → `New repository`：

- Repository name：随便起，例如 `stock-calendar`
- **必须选 Public（公开）**，Outlook 才能免登录读取
- 其他保持默认，点 `Create repository`

### 3. 上传本目录里的文件

打开新仓库首页，点 `Add file` → `Upload files`，把 `cloud` 文件夹里的**所有内容**一次性拖进去（GitHub 会自动保留 `.github/workflows/` 目录结构）：

- `listed_company_calendar.py`
- `config.json`
- `listed_company_reminders.ics`
- `.github/workflows/update-calendar.yml`

点 `Commit changes` 提交。

### 4. 等第一次自动运行

提交后 2~5 分钟内，仓库顶部 `Actions` 标签页会自动跑一次「Update Stock Calendar」。跑完（绿色对勾）后，日历文件就是最新的了。

### 5. 订阅到 Outlook

订阅地址（把用户名和仓库名换成你自己的）：

```text
https://raw.githubusercontent.com/你的用户名/你的仓库名/main/listed_company_reminders.ics
```

电脑 Outlook：日历 → 添加日历 → 从 Internet 订阅 → 粘贴这个网址。
手机 Outlook：设置 → 日历 → 添加日历 → 订阅。

之后每 6 小时云端自动更新，跟你的电脑是否开机完全无关。

## 以后怎么改股票

在仓库网页上点开 `config.json` → 铅笔图标编辑 → 改 `stocks` → `Commit changes`。
下一次自动运行（最多 6 小时内）就会生效。想立刻生效，在 `Actions` 页面点 `Run workflow`。

## 常见问题

- **为什么必须公开仓库？** Outlook 读取订阅地址时不带登录信息，私有仓库的文件它打不开。仓库里只放股票代码和日历，没有敏感信息，公开是安全的。
- **GitHub 打不开/很慢？** 可以开启 GitHub Pages 作为备用地址：仓库 `Settings` → `Pages` → `Source` 选 `Deploy from a branch` → 分支 `main`、目录 `/` → `Save`。然后订阅 `https://你的用户名.github.io/你的仓库名/listed_company_reminders.ics`。如果 GitHub 整体访问困难，告诉我，我帮你配腾讯云/七牛等国内存储（需要你有对应账号）。
- **想要更频繁刷新？** 改 `.github/workflows/update-calendar.yml` 里的 `cron`，例如每 2 小时：`0 */2 * * *`。
- **订阅报“日历格式无法识别”？** 少数情况下 GitHub 返回的内容类型不是日历格式。先告诉我，我可以给文件加个转换层，或者直接改用 Pages 地址试一下。
