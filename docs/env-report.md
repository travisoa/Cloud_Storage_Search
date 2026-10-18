# 环境核查入口

环境配置见[密钥存储与配置](credentials.md)，真实执行验收见[验证报告](test-report.md)。静态文档不作为服务、凭据或订阅当前状态的依据。

```bash
python3 scripts/manage.py status
python3 scripts/media.py doctor
python3 scripts/media.py doctor --check-quark
python3 scripts/media.py watch list
```

以上命令用于服务状态、配置、影库可读性及订阅状态检查，不写云盘。

[初期本机接入记录](archive/2026-10-17-initial-environment.md)保留早期核查结果，仅用于历史追溯。
