#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "GTAIVTrafficData.h"
#include "GTAIVTrafficManager.generated.h"

class AGTAIVTrafficCar;

/** Drop one in a level made by the GTA IV Map Exporter: it fills the streets around the player
 *  with GTA IV's cars driving its road network. */
UCLASS(Blueprintable, meta = (DisplayName = "GTA IV Traffic Manager"))
class GTAIVTRAFFIC_API AGTAIVTrafficManager : public AActor
{
	GENERATED_BODY()

public:
	AGTAIVTrafficManager();

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	TObjectPtr<UGTAIVRoadNetwork> Network;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	TObjectPtr<UGTAIVVehicleSet> VehicleSet;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	bool bTrafficEnabled = true;

	/** Cars alive at once around the player. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic", meta = (ClampMin = "0", ClampMax = "300"))
	int32 MaxCars = 40;

	/** Cars appear between MinSpawnDistance and SpawnRadius from the player (cm)... */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	float SpawnRadius = 15000.f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	float MinSpawnDistance = 3500.f;

	/** ...and are removed beyond this. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	float DespawnRadius = 22000.f;

	/** Typical speed (cm/s; 1300 = 47 km/h). Each car varies a little around it. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	float CruiseSpeed = 1300.f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	float LaneWidth = 450.f;

	/** Seconds each direction gets at a junction before the other direction goes. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	float JunctionPhaseSeconds = 16.f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Traffic")
	bool bStopForPlayer = true;

	/** Number of cars currently driving. */
	UFUNCTION(BlueprintPure, Category = "Traffic")
	int32 GetCarCount() const { return Agents.Num(); }

	/** Removes every traffic car (they respawn while traffic is enabled). */
	UFUNCTION(BlueprintCallable, Category = "Traffic")
	void ClearTraffic();

protected:
	virtual void BeginPlay() override;
	virtual void EndPlay(const EEndPlayReason::Type Reason) override;
	virtual void Tick(float DeltaSeconds) override;

private:
	struct FAgent
	{
		TWeakObjectPtr<AGTAIVTrafficCar> Car;
		TArray<FVector> Path;        // polyline ahead; Path[0] is behind the car
		TArray<int32> PathJunction;  // per point: junction node it belongs to, or -1
		float SegT = 0.f;            // distance travelled along Path[0] -> Path[1]
		float Speed = 0.f;
		float Desired = 1300.f;
		float Length = 450.f;
		float RideHeight = 60.f;
		int32 PrevNode = -1;
		int32 LastNode = -1;         // last node appended to the route
		int32 Lane = 0;
		int32 GrantedJunction = -1;  // junction this car may cross
		float StuckTime = 0.f;
		float Yaw = 0.f;
		FVector Location = FVector::ZeroVector;
		FVector Forward = FVector::ForwardVector;
	};

	TArray<FAgent> Agents;
	TArray<int32> InLinks;              // per node: number of incoming drivable links
	TArray<bool> bJunction;
	TArray<FVector> JunctionAxis;
	TMap<FIntPoint, TArray<int32>> Grid; // 100 m cells -> nodes with outgoing links
	TArray<float> VehicleWeights;
	float SpawnTimer = 0.f;
	FRandomStream Rng;

	void BuildCaches();
	bool TrySpawn(const FVector& PlayerLoc, const FVector& ViewLoc, const FVector& ViewDir);
	void ExtendPath(FAgent& A, float Ahead);
	bool AppendNextLink(FAgent& A);
	int32 ReverseLanes(int32 From, int32 To) const;
	FVector LaneOffset(int32 From, int32 To, int32 Lane) const;
	void UpdateAgent(FAgent& A, float Dt, const FVector& PlayerLoc, bool bHasPlayer, int32 Index);
	float GapAhead(const FAgent& A, int32 Index, const FVector& PlayerLoc, bool bHasPlayer, float& OutLeadSpeed) const;
	float JunctionStopDistance(FAgent& A) const;
	bool PhaseAllows(int32 Junction, const FVector& ApproachDir) const;
	void Advance(FAgent& A, float Distance);
	void PlaceCar(FAgent& A, float Dt, float Moved);
	FVector PathPoint(const FAgent& A, float S) const;
	void RemoveAgent(int32 Index);
	int32 PickVehicle();
};
