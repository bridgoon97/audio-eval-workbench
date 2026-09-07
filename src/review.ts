// 复盘视图的纯展示口径：数据由服务端 /report 统一计算，界面只负责呈现与跳转。

import type { Sample } from './types';

export type VoteRow = { ID: string; 名称: string; 票数: number };
export type SampleSummary = { 分母: number; 票数: VoteRow[]; 分歧: boolean };
export type ProgressSummary = {
  受邀评测者: number;
  已完成: number;
  进行中: number;
  未开始: number;
  已完成名单: string[];
  进行中名单: string[];
  未开始名单: string[];
  成员: { ID: string; 名称: string; 已提交片段数: number; 状态: string }[];
};
export type TagSummary = {
  标签: string;
  根评论数: number;
  片段: { ID: string; 名称: string; 根评论数: number }[];
};
export type ReportPayload = {
  任务: { id: string; title: string; kind: string; mode: string; status: string };
  样本: (Sample & SampleSummary)[];
  参与进度: ProgressSummary;
  标签汇总: TagSummary[];
  生成时间: string;
  播放口径: string;
  解释边界: string;
};

// 百分比必须写明分母（该片段的有效提交人数）；没有提交时不呈现百分比。
export function formatShare(count: number, denominator: number): string {
  if (!denominator) return '暂无提交';
  return `${((count / denominator) * 100).toFixed(1)}%（${count}/${denominator}）`;
}

// 进度条宽度；与 formatShare 同口径，分母为 0 时不显示条。
export function shareWidth(count: number, denominator: number): string {
  if (!denominator) return '0%';
  return `${(count / denominator) * 100}%`;
}

// 出现的不同偏好种类数（只统计有票的选项），用于描述分歧，不做显著性判断。
export function distinctChoices(votes: VoteRow[]): number {
  return votes.filter((vote) => vote.票数 > 0).length;
}
