import { useState } from "react";
import { Alert, Button, Form, Input } from "antd";
import { useApp } from "../store";
import type { ServerConfig } from "../config";

export function Setup() {
  const { testConnection, connect, lastError, clearError } = useApp();
  const [busy, setBusy] = useState(false);
  const [form] = Form.useForm<ServerConfig>();

  const submit = async (values: ServerConfig) => {
    setBusy(true);
    clearError();
    try {
      await testConnection(values.baseUrl, values.token);
      await connect(values);
    } catch (err) {
      // 错误已由 store 记录；这里保持停留本页
      void err;
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ maxWidth: "100%", padding: "var(--tok-spaceLg-px)" }}>
      <h2>接入 Strayt 服务器</h2>
      <p className="strayt-muted">
        首次使用：配置服务器地址 + 访问令牌。令牌是唯一准入凭据（单机自用，无账号体系）；
        API 需要时请到「设置」里配置，Key 只会存到你自己服务器上。
      </p>
      {lastError ? (
        <Alert type="error" showIcon message={lastError} style={{ marginBottom: "var(--tok-spaceMd-px)" }} />
      ) : null}
      <Form
        form={form}
        layout="vertical"
        initialValues={{ baseUrl: "http://127.0.0.1:8000", token: "" }}
        onFinish={submit}
      >
        <Form.Item
          label="服务器地址"
          name="baseUrl"
          rules={[{ required: true, message: "请输入服务器地址" }]}
        >
          <Input placeholder="http://127.0.0.1:8000" autoComplete="url" />
        </Form.Item>
        <Form.Item
          label="访问令牌"
          name="token"
          rules={[{ required: true, message: "请输入访问令牌" }]}
        >
          <Input.Password placeholder="服务端 gen_token 生成的令牌" autoComplete="off" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={busy} block>
          连接并拉取数据
        </Button>
      </Form>
    </div>
  );
}