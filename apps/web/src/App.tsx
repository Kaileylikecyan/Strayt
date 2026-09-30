import { useEffect, useState } from "react";
import { useApp } from "./store";
import { Setup } from "./pages/Setup";
import { Projects } from "./pages/Projects";
import { FilesPage } from "./pages/FilesPage";
import { JobsPage } from "./pages/JobsPage";
import { RecitePage } from "./pages/RecitePage";
import { CheckinPage } from "./pages/CheckinPage";
import { GraphPage } from "./pages/GraphPage";
import { SettingsPage } from "./pages/SettingsPage";
import { Shell, type TabKey } from "./Shell";

export function App() {
  const { config, refresh } = useApp();
  const [tab, setTab] = useState<TabKey>("projects");
  const [reciteTarget, setReciteTarget] = useState<string | null>(null);
  const [jobsTarget, setJobsTarget] = useState<string | undefined>(undefined);
  const [graphTarget, setGraphTarget] = useState<string | undefined>(undefined);

  // 打开应用（或连上服务器）时全量拉取一次，覆盖本地 snapshot
  useEffect(() => {
    if (!config) return;
    void refresh();
  }, [config, refresh]);

  if (!config) return <Setup />;

  const openRecite = (projectId: string) => {
    setReciteTarget(projectId);
    setTab("recite");
  };

  const openJobs = (projectId?: string) => {
    setJobsTarget(projectId);
    setTab("jobs");
  };

  const openGraph = (projectId?: string) => {
    setGraphTarget(projectId);
    setTab("graph");
  };

  const body =
    tab === "projects" ? (
      <Projects onOpenRecite={openRecite} onOpenGraph={openGraph} />
    ) : tab === "files" ? (
      <FilesPage onOpenJobs={openJobs} />
    ) : tab === "jobs" ? (
      <JobsPage prefillProjectId={jobsTarget} />
    ) : tab === "graph" ? (
      <GraphPage initialProjectId={graphTarget} />
    ) : tab === "recite" ? (
      <RecitePage initialProjectId={reciteTarget} />
    ) : tab === "checkin" ? (
      <CheckinPage />
    ) : (
      <SettingsPage />
    );

  return (
    <Shell tab={tab} onTab={setTab}>
      {body}
    </Shell>
  );
}