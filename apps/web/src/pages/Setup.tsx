import { useState } from "react";
import { Alert, Button, Form, Input } from "antd";
import { useApp } from "../store";

/** 表单值：口令只在表单 state 里短暂存在，提交后不落任何存储（ADR-0009）。 */
interface Credentials {
  baseUrl: string;
  password: string;
  confirm?: string;
}

export function Setup() {
  const { probeServer, authenticate, lastError, clearError } = useApp();
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<"setup" | "login" | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [form] = Form.useForm<Credentials>();

  const msg = (err: unknown) => (err instanceof Error ? err.message : String(err));

  /** 先探活 + 问服务端「有没有口令」，据此决定显示创建还是登录表单。
   *  不靠错误码反推 —— 那样首次接入会先撞一次 409 再回退，体验很怪。 */
  const probe = async () => {
    setBusy(true);
    clearError();
    setLocalError(null);
    try {
      const state = await probeServer(form.getFieldValue("baseUrl"));
      setMode(state.passwordSet ? "login" : "setup");
    } catch (err) {
      setLocalError(msg(err));
    } finally {
      setBusy(false);
    }
  };

  const submit = async (values: Credentials) => {
    if (!mode) return;
    setBusy(true);
    clearError();
    setLocalError(null);
    try {
      await authenticate(values.baseUrl, values.password, mode);
    } catch (err) {
      setLocalError(msg(err));
    } finally {
      setBusy(false);
    }
  };

  const err = localError ?? lastError;

  return (
    <div style={{ maxWidth: "100%", padding: "var(--tok-spaceLg-px)" }}>
      <h2>接入 Strayt 服务器</h2>
      <p className="strayt-muted">
        单机自用，无账号体系：<strong>一个访问口令就是全部凭证</strong>。
        首次接入时创建，之后每次输入它登录。口令只用来换一次会话令牌，不会被记住。
        API 需要时请到「设置」里配置，Key 只会存到你自己服务器上。
      </p>

      {err ? (
        <Alert type="error" showIcon message={err} style={{ marginBottom: "var(--tok-spaceMd-px)" }} />
      ) : null}

      <Form
        form={form}
        layout="vertical"
        initialValues={{ baseUrl: "http://127.0.0.1:8000", password: "" }}
        onFinish={submit}
      >
        <Form.Item
          label="服务器地址"
          name="baseUrl"
          rules={[{ required: true, message: "请输入服务器地址" }]}
        >
          <Input placeholder="http://127.0.0.1:8000" autoComplete="url" />
        </Form.Item>

        {mode === "setup" ? (
          <Form.Item
            label="设置访问口令"
            name="password"
            rules={[
              { required: true, message: "请输入访问口令" },
              { min: 8, message: "口令至少 8 位" },
            ]}
            extra={<span className="strayt-muted">至少 8 位。建议用一串你记得住的词组。</span>}
          >
            <Input.Password placeholder="设置一个访问口令" autoComplete="new-password" />
          </Form.Item>
        ) : null}

        {mode === "setup" ? (
          <Form.Item
            label="再输一次"
            name="confirm"
            dependencies={["password"]}
            rules={[
              { required: true, message: "请再输入一次口令" },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue("password") === value) return Promise.resolve();
                  return Promise.reject(new Error("两次输入不一致"));
                },
              }),
            ]}
          >
            <Input.Password placeholder="确认口令" autoComplete="new-password" />
          </Form.Item>
        ) : null}

        {mode === "login" ? (
          <Form.Item
            label="访问口令"
            name="password"
            rules={[{ required: true, message: "请输入访问口令" }]}
          >
            <Input.Password placeholder="输入访问口令" autoComplete="current-password" />
          </Form.Item>
        ) : null}

        {mode === null ? (
          <Button type="primary" block loading={busy} onClick={() => void probe()}>
            连接服务器
          </Button>
        ) : (
          <>
            <Button type="primary" htmlType="submit" block loading={busy}>
              {mode === "setup" ? "创建口令并进入" : "登录"}
            </Button>
            <Button
              type="link"
              block
              style={{ marginTop: "var(--tok-spaceSm-px)" }}
              onClick={() => {
                setMode(null);
                setLocalError(null);
                form.setFieldValue("password", "");
                form.setFieldValue("confirm", "");
              }}
            >
              返回
            </Button>
          </>
        )}
      </Form>
    </div>
  );
}