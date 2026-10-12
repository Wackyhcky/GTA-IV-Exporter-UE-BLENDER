#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "GTAIVTrafficData.h"
#include "GTAIVPedManager.generated.h"

class APawn;
class AController;
class USkeletalMesh;

/** Fills the sidewalks around the player with pedestrians walking GTA IV's streets.
 *  Sidewalks follow the road network (each street's curb on both sides). PedClass is any
 *  Character, e.g. the Game Animation Sample's SandboxCharacter_CMC: with the GM_GTAIV game
 *  mode those appear as random GTA IV peds. */
UCLASS(Blueprintable, meta = (DisplayName = "GTA IV Ped Manager"))
class GTAIVTRAFFIC_API AGTAIVPedManager : public AActor
{
	GENERATED_BODY()

public:
	AGTAIVPedManager();

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	TObjectPtr<UGTAIVRoadNetwork> Network;

	/** The character spawned for each pedestrian (needs a CharacterMovementComponent). */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	TSubclassOf<APawn> PedClass;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	bool bPedsEnabled = true;

	/** Pedestrians alive at once around the player. Animated characters are expensive. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds", meta = (ClampMin = "0", ClampMax = "200"))
	int32 MaxPeds = 25;

	/** Peds appear between MinSpawnDistance and SpawnRadius from the player (cm)... */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	float SpawnRadius = 6000.f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	float MinSpawnDistance = 1500.f;

	/** ...and are removed beyond this. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	float DespawnRadius = 9000.f;

	/** Lane width used to find the curb (same as the traffic manager's). */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	float LaneWidth = 450.f;

	/** How far past the curb peds walk (cm). */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	float SidewalkOffset = 250.f;

	/** Movement input strength (GASP walks below its analog walk threshold). */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds", meta = (ClampMin = "0.05", ClampMax = "1"))
	float WalkInput = 1.f;

	/** Ask GASP characters to walk rather than run (sets CharacterInputState.WantsToWalk). */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	bool bWalk = true;

	/** The player's look while the GTA IV ped visual override is active (Niko). */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Peds")
	TObjectPtr<USkeletalMesh> PlayerMesh;

	UFUNCTION(BlueprintPure, Category = "Peds")
	int32 GetPedCount() const { return Agents.Num(); }

	/** Removes every pedestrian (they respawn while peds are enabled). */
	UFUNCTION(BlueprintCallable, Category = "Peds")
	void ClearPeds();

protected:
	virtual void BeginPlay() override;
	virtual void EndPlay(const EEndPlayReason::Type Reason) override;
	virtual void Tick(float DeltaSeconds) override;

private:
	struct FPed
	{
		TWeakObjectPtr<APawn> Pawn;
		TWeakObjectPtr<AController> Controller;
		int32 From = -1;
		int32 To = -1;
		float Side = 1.f;        // +1 walks the right-hand sidewalk of From->To, -1 the left
		FVector Target = FVector::ZeroVector;
		FVector LastLocation = FVector::ZeroVector;
		float StuckTime = 0.f;
		float GroundZ = 0.f;
	};

	TArray<FPed> Agents;
	TArray<TArray<int32>> Adjacent;          // undirected street graph
	TMap<FIntPoint, TArray<int32>> Grid;     // 100 m cells -> nodes with streets
	float SpawnTimer = 0.f;
	float PlayerCheckTimer = 0.f;
	FRandomStream Rng;

	void BuildCaches();
	int32 LanesOut(int32 From, int32 To) const;
	FVector Sidewalk(int32 From, int32 To, float Side, bool bAtEnd) const;
	bool FindGround(const FVector& Point, FVector& OutGround) const;
	bool TrySpawn(const FVector& PlayerLoc);
	void PickNext(FPed& P);
	void SetWalkGait(APawn* Pawn) const;
	void RemoveAgent(int32 Index);
	void DressPlayer(APawn* Player) const;
};
