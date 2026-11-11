import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { endpoints } from "@/api/endpoints";
import type { ObservabilitySummary } from "@/api/types";
import { EmptyState, ErrorBox, Feedback, ICONS, ItemLink, Loading, Meter, PageHeader, SectionHeading, Segmented, StatCell, StatGrid, useWide } from "@/components/ui";
import { ms } from "@/lib/format";
import { useTheme } from "@/lib/theme";

const PALETTE = {
  light: ["#a85a32", "#d9b08c", "#6f7d8c", "#8a8f5c", "#5f8f89", "#8d7393"],
  dark: ["#e0946a", "#8f6b52", "#9aa7b6", "#b3b77f", "#7fb3ac", "#b7a3cf"],
};

export function Observability() {
  const [days, setDays] = useState(30);
  const q = useQuery({ queryKey: ["summary", days], queryFn: () => endpoints.summary(days), placeholderData: (p) => p });
  return (
    <>
      <PageHeader
        title="Observability"
        sub="How agents are using the context you maintain."
        actions={
          <Segmented
            label="Time range"
            value={days}
            onChange={setDays}
            options={[
              { id: 7, label: "7 days" },
              { id: 30, label: "30 days" },
              { id: 90, label: "90 days" },
            ]}
          />
        }
      />
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && q.data.total_sessions === 0 && (
        <EmptyState title="No agent activity in this period">
          Connect Claude or another MCP client to Satchel and ask a data question. Usage charts fill in as sessions arrive. Setup steps are in docs/integrations/claude.md.
        </EmptyState>
      )}
      {q.data && q.data.total_sessions > 0 && <Dashboard s={q.data} />}
    </>
  );
}

function Dashboard({ s }: { s: ObservabilitySummary }) {
  const [theme] = useTheme();
  const colors = PALETTE[theme];
  const wide = useWide("(min-width: 640px)");
  const clients = [...new Set(s.sessions_per_day.map((d) => d.client))].sort();
  const byDay = new Map<string, Record<string, number | string>>();
  for (const d of s.sessions_per_day) {
    const row = byDay.get(d.day) ?? { day: d.day };
    row[d.client] = d.sessions;
    byDay.set(d.day, row);
  }
  const daily = [...byDay.values()].sort((a, b) => String(a["day"]).localeCompare(String(b["day"])));
  const calls = s.top_tools.reduce((n, t) => n + t.calls, 0);
  const errors = s.top_tools.reduce((n, t) => n + t.errors, 0);
  const signals = s.signals_by_kind.reduce((n, t) => n + t.count, 0);
  const fbTotal = s.feedback.positive + s.feedback.negative;
  const maxCalls = Math.max(1, ...s.top_tools.map((t) => t.calls));
  const maxServed = Math.max(1, ...s.most_served_items.map((t) => t.served));

  return (
    <div className="space-y-9">
      <section aria-labelledby="obs-sum">
        <SectionHeading id="obs-sum" icon={ICONS.pulse} title="Summary" />
        <div className="card overflow-hidden">
          <StatGrid>
            <StatCell label="Sessions" tone="info" value={s.total_sessions.toLocaleString()} secondary={`last ${s.days} days`} info="Analyses agents ran in the selected period." />
            <StatCell label="Tool calls" glyph="ring" value={calls.toLocaleString()} secondary={`${errors} errors`} info="Calls agents made to Satchel tools, and how many returned an error." />
            <StatCell label="Signals" tone="warn" glyph="square" value={signals.toLocaleString()} secondary="gaps, errors, divergences" info="Everything agents and checks flagged in the period." />
            <StatCell
              label="Feedback"
              tone="good"
              value={fbTotal ? `${Math.round((s.feedback.positive / fbTotal) * 100)}% positive` : "–"}
              secondary={fbTotal ? `${s.feedback.positive} up · ${s.feedback.negative} down` : undefined}
              info="Share of thumbs that were positive, from people and agents."
            />
          </StatGrid>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 border-t border-line px-4 py-3 text-sm text-muted sm:px-5">
            <span className="inline-flex items-center gap-2">
              <span className="label-caps">Human</span> <Feedback up={s.feedback.human_positive} down={s.feedback.human_negative} />
            </span>
            <span className="inline-flex items-center gap-2">
              <span className="label-caps">Agent</span> <Feedback up={s.feedback.agent_positive} down={s.feedback.agent_negative} />
            </span>
          </div>
        </div>
      </section>

      <section aria-label="Sessions per day by client">
        <SectionHeading icon={ICONS.layers} title="Sessions per day, by client" />
        <div className="card h-60 p-4 sm:h-72">
          <ResponsiveContainer>
            <BarChart data={daily} margin={{ top: 4, right: 8, left: wide ? -20 : -12, bottom: 0 }}>
              <CartesianGrid vertical={false} />
              <XAxis dataKey="day" tickFormatter={(d: string) => d.slice(5)} tickLine={false} />
              <YAxis allowDecimals={false} tickLine={false} axisLine={false} />
              <Tooltip cursor={{ fill: "rgb(128 128 128 / 0.12)" }} />
              <Legend iconType="square" iconSize={9} wrapperStyle={{ fontSize: 12 }} />
              {clients.map((c, i) => (
                <Bar key={c} dataKey={c} stackId="s" fill={colors[i % colors.length]} maxBarSize={36} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      <div className="grid gap-9 xl:grid-cols-2">
        <section aria-label="Signals by kind">
          <SectionHeading icon={ICONS.alert} title="Signals by kind" />
          <div className="card p-4">
          {s.signals_by_kind.length === 0 ? (
            <p className="text-sm text-muted">No signals in this period.</p>
          ) : (
            <div style={{ height: Math.max(120, s.signals_by_kind.length * 30 + 20) }}>
              <ResponsiveContainer>
                <BarChart data={s.signals_by_kind} layout="vertical" margin={{ top: 0, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid horizontal={false} />
                  <XAxis type="number" allowDecimals={false} tickLine={false} />
                  <YAxis
                    type="category"
                    dataKey="kind"
                    width={wide ? 130 : 96}
                    tickLine={false}
                    tick={{ fontSize: 11 }}
                    tickFormatter={(k: string) => (!wide && k.length > 14 ? `${k.slice(0, 13)}…` : k)}
                  />
                  <Tooltip cursor={{ fill: "rgb(128 128 128 / 0.12)" }} />
                  <Bar dataKey="count" fill={colors[0]} radius={[0, 2, 2, 0]} barSize={14} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
          </div>
        </section>

        <section aria-label="Top tools">
          <SectionHeading icon={ICONS.bolt} title="Top tools" />
          <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-line">
                <tr>
                  <th className="th">Tool</th>
                  <th className="th hidden sm:table-cell" />
                  <th className="th text-right">Calls</th>
                  <th className="th text-right">Err</th>
                  <th className="th text-right">Avg</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {s.top_tools.map((t) => (
                  <tr key={t.tool}>
                    <td className="td font-mono [overflow-wrap:anywhere]">{t.tool}</td>
                    <td className="td hidden w-24 sm:table-cell">
                      <Meter value={(t.calls / maxCalls) * 100} label={`${t.tool} share of calls`} />
                    </td>
                    <td className="td text-right tabular-nums">{t.calls}</td>
                    <td className={`td text-right tabular-nums ${t.errors ? "text-bad" : "text-muted"}`}>{t.errors}</td>
                    <td className="td text-right font-mono text-muted">{ms(t.avg_latency_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          </div>
        </section>
      </div>

      <section aria-label="Most served items">
        <SectionHeading icon={ICONS.book} title="Most-served items" />
        {s.most_served_items.length === 0 ? (
          <div className="card p-4 text-sm text-muted">No context was served in this period.</div>
        ) : (
          <div className="card overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-line">
                  <tr>
                    <th className="th w-full">Item</th>
                    <th className="th hidden md:table-cell">Type</th>
                    <th className="th hidden sm:table-cell" />
                    <th className="th text-right">Served</th>
                    <th className="th hidden text-right sm:table-cell">Feedback</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {s.most_served_items.map((i) => (
                    <tr key={i.key}>
                      <td className="td">
                        <ItemLink itemKey={i.key} />
                      </td>
                      <td className="td hidden md:table-cell">
                        <span className="badge font-mono">{i.type}</span>
                      </td>
                      <td className="td hidden w-40 sm:table-cell">
                        <Meter value={(i.served / maxServed) * 100} label={`${i.key} share of served`} />
                      </td>
                      <td className="td text-right tabular-nums">{i.served}</td>
                      <td className="td hidden text-right sm:table-cell">
                        <Feedback up={i.thumbs_up} down={i.thumbs_down} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
