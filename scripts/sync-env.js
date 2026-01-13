#!/usr/bin/env node

/**
 * Sync global .env and .env.example files to all stack directories.
 *
 * This script reads stacks/.env (global) and merges it with stacks/<stack>/.env (stack-specific)
 * The stack-specific .env file is backed up as .env.local before merging.
 */

const fs = require("fs");
const path = require("path");

/**
 * Main sync function
 */
function syncEnvFiles() {
  const stacksDir = path.join(__dirname, "../stacks");

  if (!fs.existsSync(stacksDir)) {
    console.error(`Error: ${stacksDir} does not exist`);
    return;
  }

  const globalEnv = path.join(stacksDir, ".env");

  // Check if global .env exists
  if (!fs.existsSync(globalEnv)) {
    console.error(`Error: Global .env file not found at ${globalEnv}`);
    console.log("Please create stacks/.env file first");
    return;
  }

  // Read global .env file
  const globalEnvContent = fs.readFileSync(globalEnv, "utf8");

  // Find all stack directories
  const stackDirs = fs
    .readdirSync(stacksDir)
    .map((name) => path.join(stacksDir, name))
    .filter((dir) => fs.statSync(dir).isDirectory());

  if (stackDirs.length === 0) {
    console.log("No stack directories found");
    return;
  }

  const stackNames = stackDirs.map((dir) => path.basename(dir));
  console.log(`Found ${stackDirs.length} stack(s): ${stackNames.join(", ")}`);
  console.log();

  for (const stackDir of stackDirs) {
    const stackName = path.basename(stackDir);
    console.log(`Processing ${stackName}...`);

    const stackEnv = path.join(stackDir, ".env");
    const stackEnvLocal = path.join(stackDir, ".env.local");

    // Backup current .env to .env.local if it exists and .env.local doesn't
    if (fs.existsSync(stackEnv) && !fs.existsSync(stackEnvLocal)) {
      fs.copyFileSync(stackEnv, stackEnvLocal);
      console.log(`  ✓ Backed up .env to .env.local`);
    }

    // Read stack-specific content from .env.local
    const stackEnvContent = fs.existsSync(stackEnvLocal)
      ? fs.readFileSync(stackEnvLocal, "utf8")
      : "";

    // Merge: global + stack-specific
    let mergedEnv = globalEnvContent;
    if (stackEnvContent.trim()) {
      mergedEnv += "\n" + stackEnvContent;
    }

    // Write merged .env file
    fs.writeFileSync(stackEnv, mergedEnv, "utf8");
    console.log("  ✓ Synced .env")

    console.log();
  }

  console.log("✓ All stacks synced successfully!");
  console.log("\nNote: Edit stacks/.env for global variables");
  console.log("      Edit stacks/<stack>/.env.local for stack-specific variables");
  console.log("      Run 'npm run sync-env' to regenerate merged .env files");
}

// Run the sync
syncEnvFiles();
