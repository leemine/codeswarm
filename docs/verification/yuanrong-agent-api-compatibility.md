# 元戎 Agent API 兼容

## 原因与范围

Router要求function_version_urn的逻辑来自b65b3258（早于PR #34）。
当前通过Agent API创建／连接实例，不走旧函数invoke接口，不能靠回退PR #34解决此项。
实例running轮询来自d53a5720；当前底座某些响应缺status，需显式应用探测。
只修改Swarm客户端，不修改元戎服务端、函数部署包或账户注册。

## Agent API 与函数调用分开

agentos_router只要求frontend_endpoint；其YuanrongFrontendAgentClient使用
`require_function_urn=False`连接。其他调用者默认True保持原校验；无URN调用
真正的函数invoke仍明确报错。创建、用户路由和回收过程保留。

## 可选应用就绪

```yaml
gateway:
  agentos:
    builtin_ws_readiness: true
```

该项属于配置的gateway.agentos段，默认false。仅内置jiuwenswarm实例适用，
第三方agent继续使用既有路径。匹配实例GET成功且缺status时，通过该实例专有WebSocket
收到connection.ack且payload.status为ready才确认就绪；明确pending／failed不被覆盖。
探测共享原总超时，取消透传；不伪造running、不把仅实例存在当作可用。

## 验证范围

覆盖可选URN与真实invoke拒绝、缺status严格默认／成功探测、实例ID错配、pending／failed、
探测失败／取消与第三方agent不探测；连同原Router及元戎客户端用例运行。
当前为本地提交准备，正式CI及新提交真实Web验证在后续步骤进行。

本地结果：Router、元戎客户端、显式应用就绪和认证组合182项通过。配置默认关闭探测；源码补丁与已验证demo一致，正式新SHA端到端仍待后续。
