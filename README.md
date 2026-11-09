# 面试搭子 · AI 模拟面试

一个会走完整面试流程、并在结束后给出**每条评分都能指回你原话**的 AI 面试练习工具。

- 示例简历一键体验
- 对话式追问：按你缺的地方问，不按固定剧本
- 报告里每条依据用代码回查原文，编造的引用直接作废

## 本地运行

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
./venv/bin/streamlit run app.py
```

## 部署

- Streamlit Community Cloud：入口 `app.py`，Secrets 填 `DEEPSEEK_API_KEY`，`PUBLIC_DEMO=true`，`DAILY_CAP_CNY=10`
- 也带 `Dockerfile` / `docker-compose.yml`，可部署到任意 Docker 主机

## 许可

题库内容来自 [archlizheng/AIPM-Wiki](https://github.com/archlizheng/AIPM-Wiki)，CC BY-NC-SA 4.0，仅用于非商业学习。
