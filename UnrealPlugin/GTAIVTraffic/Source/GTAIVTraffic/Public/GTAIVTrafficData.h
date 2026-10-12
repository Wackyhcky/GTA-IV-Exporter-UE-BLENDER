#pragma once

#include "CoreMinimal.h"
#include "Engine/DataAsset.h"
#include "GTAIVTrafficData.generated.h"

class UStaticMesh;

/** One wheel of a vehicle. Offsets are in the body mesh's own space (cm). */
USTRUCT(BlueprintType)
struct FGTAIVWheel
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TObjectPtr<UStaticMesh> Mesh = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	FVector Offset = FVector::ZeroVector;

	/** Right-side wheels use the same mesh turned 180 degrees. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	bool bRightSide = false;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	bool bSteers = false;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	float Radius = 30.f;
};

/** A paint combination from carcols.dat (linear colours). */
USTRUCT(BlueprintType)
struct FGTAIVPaint
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	FLinearColor Primary = FLinearColor::White;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	FLinearColor Secondary = FLinearColor::White;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	FLinearColor Tertiary = FLinearColor::White;
};

USTRUCT(BlueprintType)
struct FGTAIVVehicleDef
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	FName Name;

	/** Body mesh as exported: the car's nose points along the mesh's -Y axis. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TObjectPtr<UStaticMesh> Body = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TArray<FGTAIVWheel> Wheels;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TArray<FGTAIVPaint> Paints;

	/** Relative spawn chance (vehicles.ide frequency). 0 = never in traffic. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	float Frequency = 100.f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	float Length = 450.f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	float Width = 190.f;

	/** Height of the body mesh origin above the ground (cm). */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	float RideHeight = 60.f;
};

/** GTA IV's cars, filled in by the exporter. */
UCLASS(BlueprintType)
class GTAIVTRAFFIC_API UGTAIVVehicleSet : public UDataAsset
{
	GENERATED_BODY()

public:
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TArray<FGTAIVVehicleDef> Vehicles;
};

/** GTA IV's vehicle road network (paths.img), in world space (cm).
 *  Node i's drivable links are LinkTarget/LinkLanes[FirstLink[i] .. FirstLink[i] + LinkCount[i]). */
UCLASS(BlueprintType)
class GTAIVTRAFFIC_API UGTAIVRoadNetwork : public UDataAsset
{
	GENERATED_BODY()

public:
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TArray<FVector> Nodes;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TArray<int32> FirstLink;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TArray<int32> LinkCount;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TArray<int32> LinkTarget;

	/** Lanes in the direction node -> target. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "GTA IV")
	TArray<int32> LinkLanes;
};
