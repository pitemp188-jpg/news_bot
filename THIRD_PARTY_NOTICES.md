# 第三方许可声明

本项目含移植自第三方开源项目的代码，按原许可要求在此声明。源码文件头部的 `来源:` 字段标明具体出处。

## NousResearch/hermes-agent

- 仓库：https://github.com/NousResearch/hermes-agent
- 许可：MIT
- 移植版本：`main@908e4a4`
- 移植范围与删改说明见 [docs/08-porting.md](docs/08-porting.md) 第 4 节。

| 源路径 | 目标文件 |
| --- | --- |
| `gateway/platforms/event.py`、`base.py`、`helpers.py` | `src/newsbot/gateway/base.py` |
| `gateway/platforms/weixin.py` | `src/newsbot/gateway/weixin.py` |
| `gateway/platforms/qqbot/adapter.py`、`constants.py`、`utils.py` | `src/newsbot/gateway/qqbot.py` |
| `gateway/pairing.py`、`gateway/platforms/access_policy_mixin.py` | `src/newsbot/gateway/auth.py`（仅参考思路） |

```
MIT License

Copyright (c) Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 仅通过依赖或接口使用，未拷贝代码

| 项目 | 用途 | 许可 |
| --- | --- | --- |
| browser-use/browser-use | 浏览器子 Agent | MIT |
| adbar/trafilatura | 正文抽取 | Apache-2.0 |
| SearXNG | 搜索服务（独立部署，仅 HTTP 调用） | AGPL-3.0 |
