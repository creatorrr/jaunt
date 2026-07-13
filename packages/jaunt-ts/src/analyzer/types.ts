export type Severity = "error" | "warning" | "info";

export interface DiagnosticRecord {
  readonly code: string;
  readonly severity: Severity;
  readonly message: string;
  readonly path?: string;
  readonly start?: number;
  readonly end?: number;
  readonly line?: number;
  readonly column?: number;
}

export interface ProjectRecord {
  readonly id: string;
  readonly configPath: string;
  readonly role: "production" | "test" | "solution";
  readonly references: readonly string[];
  readonly rootFiles: readonly string[];
  readonly compilerOptionsHash: string;
}

export interface RoutePaths {
  readonly specPath: string;
  readonly facadePath: string;
  readonly apiMirrorPath: string;
  readonly implementationPath: string;
  readonly sidecarPath: string;
  readonly contextPath?: string;
}

export interface ModuleRoute extends RoutePaths {
  readonly moduleId: string;
  readonly project: string;
  readonly packageOwner: string;
}

export interface DiscoveredSpec {
  readonly moduleId: string;
  readonly specPath: string;
  readonly project: string;
  readonly packageOwner: string;
  readonly symbols: readonly string[];
}

export interface DiscoveredTestSpec {
  readonly path: string;
  readonly project: string;
  readonly targets: readonly string[];
}

export interface DiscoveredContract {
  readonly path: string;
  readonly project: string;
  readonly symbols: readonly string[];
}

export type ArtifactKind =
  | "api-mirror"
  | "implementation"
  | "placeholder"
  | "sidecar"
  | "facade"
  | "test";

export interface ArtifactRecord {
  readonly path: string;
  readonly content: string;
  readonly sha256: string;
  readonly kind: ArtifactKind;
  readonly moduleId: string;
}

export interface OrphanRecord {
  readonly path: string;
  readonly kind: ArtifactKind;
  readonly moduleId?: string;
}

export interface SessionMetadata {
  readonly sessionId: string;
  readonly epoch: number;
  readonly snapshot: string;
  readonly inputHashes: Readonly<Record<string, string>>;
}
