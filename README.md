# AI 面试官 · 可溯源模拟面试（公开演示版）

一个会走完整面试流程、并在结束后给出**每条评分都能指回你原话**的 AI 面试练习工具。
面向 AI 产品经理校招/转行候选人。

> 这是公开演示仓库，只包含运行所需代码。
> 完整产品文档、测评数据和迭代记录不在本仓库。

## 本地运行

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # 填入 DEEPSEEK_API_KEY
./venv/bin/streamlit run app.py
```

打开 http://localhost:8501。

## 部署到 Streamlit Community Cloud

- Main file path：`app.py`
- Python：3.13
- Secrets：

```toml
DEEPSEEK_API_KEY = "sk-..."
PUBLIC_DEMO = true
DAILY_CAP_CNY = 10
```

`PUBLIC_DEMO` 会把每个浏览器会话的数据隔离，避免不同访问者互相看到简历和历史记录。

## 许可

- 题库内容来自 [archlizheng/AIPM-Wiki](https://github.com/archlizheng/AIPM-Wiki)，CC BY-NC-SA 4.0，仅用于非商业学习。
