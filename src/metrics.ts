export interface StepMetrics {
  step: number;
  startTime: number;
  endTime: number;
  durationMs: number;
  toolCalls: number;
  inputTokens: number;
  outputTokens: number;
}

export interface RunMetrics {
  challengeUrl: string;
  model: string;
  startTime: string;
  endTime: string;
  totalDurationMs: number;
  agentDurationMs: number;
  stepsCompleted: number;
  totalSteps: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  totalCost: number;
  totalToolCalls: number;
  totalApiCalls: number;
  steps: StepMetrics[];
}

// Groq pricing for GPT-OSS-120B on OpenRouter
const INPUT_COST_PER_M = 0.039;
const OUTPUT_COST_PER_M = 0.19;

export class MetricsTracker {
  private runStart = Date.now();
  private agentStart = 0;
  private agentEnd = 0;
  private steps: StepMetrics[] = [];
  private currentStep: Pick<StepMetrics, 'step' | 'startTime' | 'toolCalls' | 'inputTokens' | 'outputTokens'> | null = null;
  private totalInputTokens = 0;
  private totalOutputTokens = 0;
  private totalToolCalls = 0;
  private totalApiCalls = 0;

  startAgent() {
    this.agentStart = Date.now();
  }

  endAgent() {
    this.agentEnd = Date.now();
  }

  startStep(step: number) {
    this.currentStep = {
      step,
      startTime: Date.now(),
      toolCalls: 0,
      inputTokens: 0,
      outputTokens: 0,
    };
  }

  endStep() {
    if (!this.currentStep) return;
    const now = Date.now();
    const completed: StepMetrics = {
      step: this.currentStep.step,
      startTime: this.currentStep.startTime,
      endTime: now,
      durationMs: now - this.currentStep.startTime,
      toolCalls: this.currentStep.toolCalls,
      inputTokens: this.currentStep.inputTokens,
      outputTokens: this.currentStep.outputTokens,
    };
    this.steps.push(completed);
    this.currentStep = null;
  }

  addApiCall(inputTokens: number, outputTokens: number, toolCalls: number) {
    this.totalInputTokens += inputTokens;
    this.totalOutputTokens += outputTokens;
    this.totalToolCalls += toolCalls;
    this.totalApiCalls++;

    if (this.currentStep) {
      this.currentStep.inputTokens += inputTokens;
      this.currentStep.outputTokens += outputTokens;
      this.currentStep.toolCalls += toolCalls;
    }
  }

  getReport(challengeUrl: string, model: string, stepsCompleted: number): RunMetrics {
    const endTime = Date.now();
    const cost =
      (this.totalInputTokens / 1_000_000) * INPUT_COST_PER_M +
      (this.totalOutputTokens / 1_000_000) * OUTPUT_COST_PER_M;

    return {
      challengeUrl,
      model,
      startTime: new Date(this.runStart).toISOString(),
      endTime: new Date(endTime).toISOString(),
      totalDurationMs: endTime - this.runStart,
      agentDurationMs: (this.agentEnd || endTime) - this.agentStart,
      stepsCompleted,
      totalSteps: 30,
      totalInputTokens: this.totalInputTokens,
      totalOutputTokens: this.totalOutputTokens,
      totalCost: Math.round(cost * 10000) / 10000,
      totalToolCalls: this.totalToolCalls,
      totalApiCalls: this.totalApiCalls,
      steps: this.steps,
    };
  }

}
