#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const amuRoot = process.env.AMU_ROOT;
const engineRoot = process.env.KBB_ENGINE_ROOT;
const expectedAmuRevision = fs.readFileSync(path.join(root, "amu.lock"), "utf8").trim();
const engineLock = Object.fromEntries(fs.readFileSync(path.join(root, "engine.lock"), "utf8")
  .trim().split(/\r?\n/u).map((line) => {
    const separator = line.indexOf("=");
    if (separator <= 0) throw new Error(`invalid engine.lock line: ${line}`);
    return [line.slice(0, separator), line.slice(separator + 1)];
  }));

function fail(message) {
  console.error(`Amu service evidence build: ${message}`);
  process.exit(1);
}

function git(rootPath, args, label) {
  const result = spawnSync("git", ["-C", rootPath, ...args], { encoding: "utf8" });
  if (result.status !== 0) fail(`${label}: ${result.stderr.trim()}`);
  return result.stdout.trim();
}

function verifyPinnedCleanCheckout(checkout, expectedRevision, label) {
  if (!checkout || !fs.existsSync(path.join(checkout, ".git")))
    fail(`set ${label} to the checkout pinned in the lock files`);
  const actualRevision = git(checkout, ["rev-parse", "HEAD"], `${label} revision check failed`);
  if (actualRevision !== expectedRevision)
    fail(`${label} revision mismatch: expected ${expectedRevision}, got ${actualRevision}`);
  const dirty = git(checkout, ["status", "--porcelain", "--untracked-files=all"], `${label} status check failed`);
  if (dirty) fail(`${label} checkout must be clean at ${expectedRevision}`);
}

if (!/^[0-9a-f]{40}$/u.test(expectedAmuRevision)) fail("amu.lock must contain one full Git SHA");
if (!/^https:\/\/github\.com\/kotoba-lang\/org-babashka-nbb\.git$/u.test(engineLock.repository ?? ""))
  fail("engine.lock repository is not the approved NBB engine repository");
if (!/^[0-9a-f]{40}$/u.test(engineLock.revision ?? "") || engineLock.path !== "cli.js")
  fail("engine.lock must pin the NBB engine revision and cli.js entrypoint");

verifyPinnedCleanCheckout(amuRoot, expectedAmuRevision, "AMU_ROOT");
verifyPinnedCleanCheckout(engineRoot, engineLock.revision, "KBB_ENGINE_ROOT");
const engineCli = path.join(engineRoot, engineLock.path);
if (!fs.existsSync(engineCli)) fail(`pinned Amu engine CLI is missing: ${engineCli}`);

const outputDir = path.join(root, "dist");
const outputPath = path.join(outputDir, "service-evidence-core.mjs");
fs.mkdirSync(outputDir, { recursive: true });
const compiler = spawnSync(process.execPath, [
  path.join(amuRoot, "bin", "amu"), "compile",
  path.join(root, "src/yataverse/economy/evidence.kotoba"),
  "--target", "js-kotoba-v1", "--jvm-free", "--output", outputPath,
], { encoding: "utf8", env: { ...process.env, KBB_ENGINE: engineCli } });
if (compiler.stdout) process.stdout.write(compiler.stdout);
if (compiler.stderr) process.stderr.write(compiler.stderr);
if (compiler.status !== 0) process.exit(compiler.status ?? 1);

for (const artifact of [outputPath, `${outputPath}.manifest.json`, `${outputPath}.provenance.edn`]) {
  if (!fs.existsSync(artifact)) fail(`compiler did not write ${artifact}`);
}
const manifest = JSON.parse(fs.readFileSync(`${outputPath}.manifest.json`, "utf8"));
if (manifest["kotoba.artifact/target"] !== "js-kotoba-v1"
    || (manifest["kotoba.artifact/effects"] ?? []).length !== 0)
  fail("compiled module is not a pure js-kotoba-v1 artifact");

for (const source of [
  path.join(root, "src/yataverse/economy/evidence-host.mjs"),
  path.join(root, "src/yataverse/economy/evidence.mjs"),
]) {
  const syntax = spawnSync(process.execPath, ["--check", source], { encoding: "utf8" });
  if (syntax.stdout) process.stdout.write(syntax.stdout);
  if (syntax.stderr) process.stderr.write(syntax.stderr);
  if (syntax.status !== 0) process.exit(syntax.status ?? 1);
}

console.log(`Built pure Amu/Kotoba service evidence with ${expectedAmuRevision}`);
