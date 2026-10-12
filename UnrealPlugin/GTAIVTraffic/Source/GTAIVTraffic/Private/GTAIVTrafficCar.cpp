#include "GTAIVTrafficCar.h"

#include "Components/StaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "Materials/MaterialInstanceDynamic.h"

AGTAIVTrafficCar::AGTAIVTrafficCar()
{
	PrimaryActorTick.bCanEverTick = false;

	Root = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));
	Root->SetMobility(EComponentMobility::Movable);
	RootComponent = Root;

	Body = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("Body"));
	Body->SetupAttachment(Root);
	Body->SetMobility(EComponentMobility::Movable);
	// exported meshes point their nose along -Y; turn them to face the actor's +X
	Body->SetRelativeRotation(FRotator(0.f, 90.f, 0.f));
	Body->SetCollisionProfileName(TEXT("BlockAllDynamic"));
	// ground traces from other cars (and this one) shouldn't hit car bodies
	Body->SetCollisionResponseToChannel(ECC_Visibility, ECR_Ignore);
	Body->SetCollisionResponseToChannel(ECC_Camera, ECR_Ignore);
}

void AGTAIVTrafficCar::Setup(const FGTAIVVehicleDef& Def, const FGTAIVPaint& Paint)
{
	VehicleName = Def.Name;
	Body->SetStaticMesh(Def.Body);
	WheelDefs = Def.Wheels;
	WheelRoll.Init(0.f, WheelDefs.Num());

	// paint materials are named MI_VEH_<texture>_P<slot>: give each its colour from the combo
	auto ApplyPaint = [&Paint](UStaticMeshComponent* C)
	{
		for (int32 m = 0; m < C->GetNumMaterials(); ++m)
		{
			const UMaterialInterface* Mat = C->GetMaterial(m);
			if (!Mat)
			{
				continue;
			}
			const FString N = Mat->GetName();
			const int32 At = N.Find(TEXT("_P"), ESearchCase::CaseSensitive, ESearchDir::FromEnd);
			if (At == INDEX_NONE || At + 2 >= N.Len() || !FChar::IsDigit(N[At + 2]))
			{
				continue;
			}
			const int32 Slot = N[At + 2] - TEXT('0');
			const FLinearColor& Col = Slot == 2 ? Paint.Secondary : Slot == 3 ? Paint.Tertiary : Paint.Primary;
			if (UMaterialInstanceDynamic* MID = C->CreateDynamicMaterialInstance(m))
			{
				MID->SetVectorParameterValue(TEXT("PaintColor"), Col);
			}
		}
	};
	ApplyPaint(Body);

	for (int32 i = 0; i < WheelDefs.Num(); ++i)
	{
		const FGTAIVWheel& W = WheelDefs[i];
		UStaticMeshComponent* C = NewObject<UStaticMeshComponent>(this, *FString::Printf(TEXT("Wheel%d"), i));
		C->SetMobility(EComponentMobility::Movable);
		C->SetStaticMesh(W.Mesh);
		C->SetCollisionEnabled(ECollisionEnabled::NoCollision);
		C->SetupAttachment(Body);
		C->SetRelativeLocation(W.Offset);
		C->SetRelativeRotation(FRotator(0.f, W.bRightSide ? 180.f : 0.f, 0.f));
		C->RegisterComponent();
		ApplyPaint(C);
		WheelComponents.Add(C);
	}
}

void AGTAIVTrafficCar::UpdateWheels(float DistanceCm, float SteerDegrees)
{
	for (int32 i = 0; i < WheelComponents.Num(); ++i)
	{
		const FGTAIVWheel& W = WheelDefs[i];
		const float Radius = FMath::Max(W.Radius, 5.f);
		// rolling: the axle is the body mesh's X axis
		WheelRoll[i] = FMath::Fmod(WheelRoll[i] + FMath::RadiansToDegrees(DistanceCm / Radius), 360.f);
		const FQuat Side(FVector::UpVector, FMath::DegreesToRadians(W.bRightSide ? 180.f : 0.f));
		const FQuat Steer(FVector::UpVector, FMath::DegreesToRadians(W.bSteers ? -SteerDegrees : 0.f));
		const FQuat Roll(FVector::ForwardVector, FMath::DegreesToRadians(W.bRightSide ? -WheelRoll[i] : WheelRoll[i]));
		WheelComponents[i]->SetRelativeRotation(Steer * Side * Roll);
	}
}
