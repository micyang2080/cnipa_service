import sys
import json

# 读取 JSON 输入
with open('test_input.json', 'r', encoding='utf-8-sig') as f:
    args = json.load(f)

# 导入并运行
sys.path.insert(0, '.')
from run_cnipa_login_pw import main

# 覆盖 stdin
import io
sys.stdin = io.StringIO(json.dumps(args))

main()
