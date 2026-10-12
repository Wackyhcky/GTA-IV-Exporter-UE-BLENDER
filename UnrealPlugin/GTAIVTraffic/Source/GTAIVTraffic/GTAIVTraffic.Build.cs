using UnrealBuildTool;

public class GTAIVTraffic : ModuleRules
{
	public GTAIVTraffic(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
		PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine", "AIModule" });
	}
}
